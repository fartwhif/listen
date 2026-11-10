FROM nginx:1.27-alpine

RUN apk add --no-cache wireguard-tools iproute2

WORKDIR /usr/share/nginx/html

COPY index.html .
COPY nginx/default.conf /etc/nginx/conf.d/default.conf

RUN mkdir -p /etc/wireguard
COPY wireguard/wg0.conf.template /etc/wireguard/wg0.conf.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/entrypoint.sh"]
