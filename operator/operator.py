#!/usr/bin/env python

from kubeoperative import KubeOperative

from resourceclaimhandler import ResourceClaimHandler
from resourcehandlehandler import ResourceHandleHandler

import prometheus_client

ko = KubeOperative()
claim_handler = ResourceClaimHandler(ko)
handle_handler = ResourceHandleHandler(ko)

@ko.watcher(
    name='ResourceProvider',
    namespace=ko.operator_namespace,
    plural='resourceproviders',
    group=ko.operator_domain,
    version='v1',
    preload=True,
    cache_resources=True
)
def watch_providers(event):
    # Handlers are only watched to take advantage of the cache
    pass

@ko.watcher(
    name='ResourceClaim',
    plural='resourceclaims',
    group=ko.operator_domain,
    version='v1'
)
def watch_claims(event):
    claim = event.resource
    if event.added:
        claim_handler.added(claim)
    elif event.deleted:
        if event.delete_finalized:
            ko.logger.info(
                'ResourceClaim %s/%s deleted',
                claim['metedata']['namespace'],
                claim['metedata']['name']
            )
        else:
            claim_handler.deleted(claim)
    elif event.modified:
        claim_handler.modified(claim)

@ko.watcher(
    name='ResourceHandle',
    namespace=ko.operator_namespace,
    plural='resourcehandles',
    group=ko.operator_domain,
    version='v1'
)
def watch_handles(event):
    handle = event.resource
    if event.added:
        handle_handler.added(handle)
    elif event.deleted:
        if event.delete_finalized:
            ko.logger(
                'ResourceHandle %s deleted',
                handle['metedata']['name']
            )
        else:
            handle_handler.deleted(handle)
    elif event.modified:
        handle_handler.modified(handle)

def main():
    """Main function."""
    ko.start_watchers()
    prometheus_client.start_http_server(8000)

if __name__ == '__main__':
    main()
