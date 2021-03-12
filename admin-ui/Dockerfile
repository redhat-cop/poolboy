FROM node:15.11.0-alpine AS builder
WORKDIR /app
COPY . /app
RUN ./build.sh

FROM registry.access.redhat.com/rhscl/httpd-24-rhel7:2.4
COPY --from=builder /app/build /var/www/html
EXPOSE 8080
