FROM nginx:alpine

COPY . /usr/share/nginx/html/

# app.html is the app shell (generic "Mizan.ai" title, and common.js
# already redirects to login.html if there's no auth token) -- there's no
# index.html in this repo, so without this nginx:alpine's own default
# index.html (the "Welcome to nginx!" page) would keep being served at /
# since COPY never deletes it.
COPY app.html /usr/share/nginx/html/index.html

# config.js is the local-dev version (points at localhost:PORT per
# service) -- config.prod.js is the same file with every base URL emptied
# out to same-origin, for when this image is served from behind the
# Ingress at mizanai.org. Overwritten after the wholesale copy above so
# the image always ships the production config.
COPY config.prod.js /usr/share/nginx/html/config.js

EXPOSE 80
