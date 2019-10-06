FROM registry.access.redhat.com/ubi8:latest

USER 0

RUN yum install -y \
      gcc \
      openssl-devel \
      bzip2-devel \
      libffi-devel \
      make \
      nss_wrapper \
    && \
    curl https://www.python.org/ftp/python/3.7.4/Python-3.7.4.tgz \
      -o /usr/local/src/Python-3.7.4.tgz && \
    tar zxf /usr/local/src/Python-3.7.4.tgz -C /usr/local/src && \
    cd /usr/local/src/Python-3.7.4 && \
    ./configure --enable-optimizations && \
    make altinstall && \
    cd /usr/local/bin && \
    rm /usr/local/src/Python-3.7.4.tgz && \
    ln -s idle3.7 idle3 && \
    ln -s python3.7 python3 && \
    ln -s pip3.7 pip3 && \
    ln -s pydoc3.7 pydoc3 && \
    pip3 install --upgrade \
      ansible \
      inflection \
      jmespath \
      jsonpatch \
      kopf \
      kubernetes \
      ndg-httpsclient \
      openapi-core \
      prometheus_client \
      pyasn1 \
      pyOpenSSL \
      werkzeug \
    && \
    mkdir -p /operator/nss && \
    chmod a+rwx /operator/nss

COPY run.sh poolboy.py /operator/
COPY gpte /operator/gpte

USER 1000

CMD /operator/run.sh
