FROM node:20-alpine AS builder
WORKDIR /app
# Change this line in your Dockerfile:
ARG NEXT_PUBLIC_API_BASE=http://localhost:3500/v1.0/invoke/carqna-service/method/agent

ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-alpine
WORKDIR /app
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/package*.json ./
RUN npm ci --production
EXPOSE 3000
CMD ["npm", "run", "start"]
