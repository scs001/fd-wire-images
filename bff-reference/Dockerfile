# Reference BFF for the wire REST data plane (wire-customer-data-engine 7.2).
# Pack-ready: non-root, HEALTHCHECK on /bff/me, key via env only.
FROM node:22-alpine
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev && npm cache clean --force
COPY server.js ./
COPY public ./public
RUN addgroup -S bff && adduser -S bff -G bff && chown -R bff:bff /app
USER bff
ENV PORT=8787
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8787/bff/me || exit 1
CMD ["node", "server.js"]
