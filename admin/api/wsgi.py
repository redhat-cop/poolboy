#!/usr/bin/env python3

import flask
import json
import kubernetes
import os
import paste.translogger
import pathlib
import random
import re
import redis
import string
import waitress

def random_string(length):
    return ''.join([random.choice(string.ascii_letters + string.digits) for n in range(length)])

import logging
logger = logging.getLogger('waitress')
logger.setLevel(logging.INFO)

application = flask.Flask(__name__)
redis_connection = None
session_token_cache = {}
session_token_lifetime = int(os.environ.get('SESSION_LIFETIME', 600))

if 'REDIS_PASSWORD' in os.environ:
    redis_connection = redis.StrictRedis(
        host = os.environ.get('REDIS_SERVER', 'redis'),
        port = int(os.environ.get('REDIS_PORT', 6379)),
        password = os.environ.get('REDIS_PASSWORD'),
        charset = 'utf-8',
        db = 0,
        decode_responses = True,
    )

if os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/namespace'):
    kubernetes.config.load_incluster_config()
else:
    kubernetes.config.load_kube_config()

core_v1_api = kubernetes.client.CoreV1Api()
custom_objects_api = kubernetes.client.CustomObjectsApi()

def proxy_user():
    user = flask.request.headers.get('X-Forwarded-User')
    if not user:
        user = os.environ.get('DEV_UNAUTHENTICATED_USER')
    if not user:
        flask.abort(401, description="No X-Forwarded-User header")
    return user

def proxy_user_api_client(user):
    return kubernetes.client.ApiClient(
        header_name = 'Impersonate-User',
        header_value = user
    )

def set_session_token(user):
    token = random_string(32)
    if redis_connection:
        redis_connection.setex(token, session_token_lifetime, user)
    else:
        session_token_cache[token] = user
    return token

def verify_api_token(user):
    authentication_header = flask.request.headers.get('Authentication')
    if not authentication_header:
        flask.abort(401, description='No Authentication header')
    if not authentication_header.startswith('Bearer '):
        flask.abort(401, description='Authentication header is not a bearer token')
    token = authentication_header[7:]
    if redis_connection:
        if redis_connection.get(token) != user:
            raise Exception(redis_connection.get(token) + ' != ' + user)
            flask.abort(401, description='Invalid bearer token')
    elif user != session_token_cache.get(token):
        flask.abort(401, description='Invalid bearer token')

@application.route("/session")
def session_token():
    user = proxy_user()
    return flask.jsonify({
        "token": set_session_token(user),
        "lifetime": session_token_lifetime,
    })

@application.route("/apis/<path:path>")
def apis_proxy(path):
    user = proxy_user()
    verify_api_token(user)
    api_client = proxy_user_api_client(user)
    try:
        (data) = api_client.call_api(
            flask.request.path,
            flask.request.method,
            auth_settings = ['BearerToken'],
            query_params = [ (k, v) for k, v in flask.request.args.items() ],
            response_type = 'object',
            _return_http_data_only = True
        )
        return flask.jsonify(data)
    except kubernetes.client.rest.ApiException as e:
        if e.status == 403:
            flask.abort(e.status, description=e.reason)

def send_static_file(path):
    static_path = pathlib.Path(application.static_folder).resolve()
    file_path = (static_path / path).resolve()
    if not static_path in file_path.resolve().parents:
        flask.abort(403)
    try:
        return flask.send_file(str(file_path))
    except FileNotFoundError:
        flask.abort(404)

# Paths handled by vue
@application.route('/')
@application.route('/about')
@application.route('/resourceclaims')
@application.route('/resourceclaim/<string>')
@application.route('/resourcehandles')
@application.route('/resourcehandle/<string>')
@application.route('/resourcepools')
@application.route('/resourcepool/<string>')
@application.route('/resourceproviders')
@application.route('/resourceprovider/<string>')
def vue_path():
    return send_static_file('index.html')

# Anything else must be static
@application.route('/<path:path>')
def catch_all(path):
    return send_static_file(path)

if __name__ == "__main__":
    waitress.serve(
        paste.translogger.TransLogger(application, setup_console_handler=False),
        listen='0.0.0.0:5000'
    )
