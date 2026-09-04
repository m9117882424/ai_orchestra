FROM nginx:1.27-alpine

COPY default.conf.template /etc/nginx/templates/orchestra.conf.template

CMD ["/bin/sh", "-c", "envsubst '$MODEL_ROUTER_CLIENT_KEY $MODEL_ROUTER_MASTER_KEY' < /etc/nginx/templates/orchestra.conf.template > /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'"]
