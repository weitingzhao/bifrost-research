# bifrost pine-runner (AGPL-3.0-only, links PineTS). Built by bifrost-deliver-research
# as bifrost-research-pine:<tag>; deployed as its own Deployment in the research namespace.
FROM node:22-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY server.mjs LICENSE.md ./
USER node
ENV PORT=8797
EXPOSE 8797
CMD ["node", "server.mjs"]
