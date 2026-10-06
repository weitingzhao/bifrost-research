# bifrost pine-runner (AGPL-3.0-only, links PineTS). Built by bifrost-build-research-pine
# as bifrost-research-pine:<tag>; deployed as its own Deployment in the research namespace.
FROM node:22-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY server.mjs LICENSE.md ./
USER node
ENV PORT=8797
EXPOSE 8797
# Pine sources run as JS in this process. The permission model confines them:
# reads only the app's own files, no fs writes, child processes, workers, addons
# or WASI (Node 22 has no network permission; the NetworkPolicy denies egress).
CMD ["node", "--permission", \
     "--allow-fs-read=/app/server.mjs", \
     "--allow-fs-read=/app/package.json", \
     "--allow-fs-read=/app/node_modules/", \
     "server.mjs"]
