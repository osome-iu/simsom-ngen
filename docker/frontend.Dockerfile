# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

FROM node:22-bookworm-slim AS frontend-builder

WORKDIR /app
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci

COPY web/frontend/ ./
RUN npm run build


FROM nginx:1.27-alpine

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-builder /app/dist /usr/share/nginx/html

EXPOSE 80
