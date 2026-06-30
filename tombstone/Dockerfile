# Minimal "this image was retired" tombstone for the old Docker Hub repo
# (stevezzau/plex_generate_vid_previews). Built + pushed ONCE by
# .github/workflows/retire-deprecated-image.yml, then the deprecated build is
# removed from ci.yml. Anyone who pulls the old image after that gets a page
# telling them to switch to stevezzau/media_preview_generator.
FROM nginx:1.27-alpine
COPY index.html /usr/share/nginx/html/index.html
COPY default.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
HEALTHCHECK CMD wget -qO- http://127.0.0.1:8080/ >/dev/null 2>&1 || exit 1
