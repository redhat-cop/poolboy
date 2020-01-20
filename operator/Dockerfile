FROM registry.access.redhat.com/ubi8:latest

USER 0

COPY files /operator

ENV PYTHON_VERSION=3.7.6

RUN yum install -y \
      gcc \
      openssl-devel \
      bzip2-devel \
      libffi-devel \
      make \
      nss_wrapper \
    && \
    /operator/python3-install.sh && \
    pip3 install --upgrade -r /operator/requirements.txt && \
    mkdir -p /operator/nss && \
    chmod a+rwx /operator/nss

USER 1000

CMD /operator/run.sh
