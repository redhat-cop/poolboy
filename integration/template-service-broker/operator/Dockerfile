#FROM registry.access.redhat.com/ubi8/python-37:latest
FROM docker.io/library/python:3.7

USER 0

RUN apt-get update && \
    apt-get install -y libnss-wrapper && \
    pip3 install --upgrade \
      ansible \
      inflection \
      jmespath \
      kopf \
      kubernetes \
      ndg-httpsclient \
      openapi-core \
      prometheus_client \
      pyasn1 \
      pyOpenSSL \
      werkzeug \
    && \
    mkdir -p /operator/tmp && \
    chmod a+rwx /operator/tmp

COPY run.sh poolboy-tsb.py /operator/

USER 1000

CMD /operator/run.sh
