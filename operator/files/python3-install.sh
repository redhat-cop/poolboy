#!/bin/sh

PYTHON_VERSION=${PYTHON_VERSION:-3.7.6}
PYTHON_MAJOR_VERSION=$(echo ${PYTHON_VERSION} | sed 's/\.[0-9]*$//')

#Exit on error
set -e

curl https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz \
  -o /usr/local/src/Python-${PYTHON_VERSION}.tgz
tar zxf /usr/local/src/Python-${PYTHON_VERSION}.tgz -C /usr/local/src
cd /usr/local/src/Python-${PYTHON_VERSION}
./configure --enable-optimizations
make altinstall
cd /usr/local/bin
rm /usr/local/src/Python-${PYTHON_VERSION}.tgz
ln -s idle${PYTHON_MAJOR_VERSION} idle3
ln -s python${PYTHON_MAJOR_VERSION} python3
ln -s pip${PYTHON_MAJOR_VERSION} pip3
ln -s pydoc${PYTHON_MAJOR_VERSION} pydoc3
