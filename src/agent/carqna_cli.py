"""Local runner for the automobile help agent - CarQnA.

Run from project root:
    python -m agent.carqna_cli --session <name>
"""

import argparse
import asyncio
from datetime import datetime, timezone
import os
import sys
from pathlib import Path

import aiosqlite

# Ensure we can import from src
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.graph import create_graph
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_core.messages import HumanMessage


def _get_cli_sqlite_path() -> str:
    """Local SQLite file for the CLI's checkpointer + sessions table.

    Deliberately separate from the web path's shared Postgres (see
    .plans/006-2026-08-11-session-management-plan-DONE.md) -- this file
    lives in the CLI user's own home directory, so there's no shared resource
    left for concurrent CLI users to collide on.
    """
    default_path = str(Path.home() / ".carqna" / "carqna_cli.sqlite")
    path = os.getenv("CARQNA_CLI_SQLITE_PATH", default_path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path


async def _create_session_table(conn):
    await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT NOT NULL UNIQUE,
                access_ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

async def _list_sessions(conn):
    await _create_session_table(conn)

    async with conn.execute(
            "SELECT session_id, session_name, access_ts FROM sessions ORDER BY access_ts DESC") as cursor:
        return await cursor.fetchall()

async def _get_or_create_session(conn, session_name: str) -> int:
    """Find-or-create a session by name in the local `sessions` table.

    DDL created on the fly here, not via an infra init script -- unlike the
    shared Postgres path (infrastructure/docker/postgres/initdb.d/), this
    SQLite file has no provisioning phase; the CLI itself is the only thing
    that will ever touch it. No user_id column -- the file itself is already
    scoped to one person, so there's nothing left to namespace by.
    """
    await _create_session_table(conn)

    async with conn.execute(
        "SELECT session_id FROM sessions WHERE session_name = ?", (session_name,)
    ) as cursor:
        rows = await cursor.fetchone()
    if rows is not None:
        row = rows[0]
        # Update last accessed timestamp
        await conn.execute(
            "UPDATE sessions SET access_ts = CURRENT_TIMESTAMP WHERE session_id = ?",
            (row,),
        )
    else:
        cursor = await conn.execute(
            "INSERT INTO sessions (session_name) VALUES (?)", (session_name,)
        )
        row = cursor.lastrowid
    await conn.commit()
    return row 

async def main():
    """Run the automobile help agent locally with a per-machine SQLite checkpointer."""
    parser = argparse.ArgumentParser(description="CarQnA local CLI")
    parser.add_argument(
        "--session",
        help="Session name to continue if it exists, or create if it doesn't. "
        "Required unless --list is given.",
    )
    parser.add_argument(
            "--list",
            action="store_true",
            help="List all session names and exit",
        )

    args = parser.parse_args()
    if not args.list and not args.session:
        parser.error("--session is required unless --list is given")

    print("🚗 CarQnA - Automobile Help Assistant")
    print("=" * 50)

    sqlite_path = _get_cli_sqlite_path()

    if args.list:
        async with aiosqlite.connect(sqlite_path) as conn:
            sessions = await _list_sessions(conn)
            if not sessions:
                print("No sessions yet.")
            else:
                print("Sessions:")
                for _, session_name, access_ts in sessions:
                    accessed_utc = datetime.strptime(access_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - accessed_utc).total_seconds()
                    print(f"  {session_name}  (last accessed={elapsed} seconds ago)")
        sys.exit(0)

    print("Initializing agent...")
    try:
        # Async context manager for proper AsyncSqliteSaver lifecycle (same
        # pattern as the Postgres path used before this -- setup() itself is
        # called lazily/internally by the saver on first read/write, unlike
        # AsyncPostgresSaver which needs it called explicitly).
        async with AsyncSqliteSaver.from_conn_string(sqlite_path) as checkpointer:
            session_id = await _get_or_create_session(checkpointer.conn, args.session)
            agent = await create_graph(checkpointer=checkpointer)

            # No user_id prefix needed -- see _get_or_create_session's docstring.
            thread_id = str(session_id)
            config = {"configurable": {"thread_id": thread_id}}

            print(f"✓ Agent ready (session: {args.session!r}, thread_id: {thread_id})")
            print(f"  Local store: {sqlite_path}")
            print("=" * 50)
            print("Ask your automobile questions. Type '/quit' to exit.\n")

            while True:
                try:
                    user_input = input("You: ").strip()
                    if user_input in ("/quit", "/exit", "/q"):
                        print("Goodbye!")
                        break

                    if not user_input:
                        continue

                    print("\nAgent: Thinking...\n")

                    # Use ainvoke for async invocation with proper message format
                    result = await agent.ainvoke(
                        {"messages": [HumanMessage(content=user_input)]},
                        config=config,
                    )

                    # Extract and print response
                    if isinstance(result, dict) and "messages" in result:
                        # Get the last message (the agent's response)
                        messages = result["messages"]
                        if messages:
                            last_msg = messages[-1]
                            # Handle both string and AIMessage objects
                            content = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
                            print(f"Agent: {content}\n")
                    elif isinstance(result, dict):
                        # Check for common output keys
                        for key in ["output", "result", "response"]:
                            if key in result:
                                print(f"Agent: {result[key]}\n")
                                break
                        else:
                            # If no recognized key, print the whole result
                            print(f"Agent: {result}\n")
                    else:
                        print(f"Agent: {result}\n")

                except KeyboardInterrupt:
                    print("\nGoodbye!")
                    break
                except Exception as e:
                    print(f"Error: {e}\n")
                    import traceback
                    traceback.print_exc()
    except Exception as e:
        print(f"❌ Failed to initialize agent: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
