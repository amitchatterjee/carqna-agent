"""Local runner for the automobile help agent - CarQnA.

Run from project root:
    python -m agent.carqna
"""

import asyncio
import sys
from pathlib import Path

# Ensure we can import from src
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.graph import create_graph, _get_checkpointer_conn_string
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langchain_core.messages import HumanMessage


async def main():
    """Run the automobile help agent locally with Postgres checkpointing."""
    print("🚗 CarQnA - Automobile Help Assistant")
    print("=" * 50)

    # Get database connection string and create checkpointer context manager
    conn_string = _get_checkpointer_conn_string()

    # Use async context manager for proper AsyncPostgresSaver lifecycle
    print("Initializing agent...")
    try:
        async with AsyncPostgresSaver.from_conn_string(conn_string) as checkpointer:
            await checkpointer.setup()
            agent = await create_graph(checkpointer=checkpointer)
            
            # Use a persistent thread for multi-turn conversation
            thread_id = "carqna-local-session"
            config = {"configurable": {"thread_id": thread_id}}

            print(f"✓ Agent ready (thread_id: {thread_id})")
            print("=" * 50)
            print("Ask your automobile questions. Type 'quit' to exit.\n")

            while True:
                try:
                    user_input = input("You: ").strip()
                    if user_input.lower() in ("quit", "exit", "q"):
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
