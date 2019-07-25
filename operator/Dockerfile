FROM registry.access.redhat.com/ubi8/python-36:latest

USER 0

RUN pip install --ignore-installed \
      ansible \
      inflection \
      jmespath \
      jsonpatch \
      kubernetes \
      ndg-httpsclient \
      openapi-core \
      prometheus_client \
      pyasn1 \
      pyOpenSSL \
      werkzeug \
    && \
    mkdir /operator

COPY *.py /operator/

USER 1000

CMD /operator/operator.py
