#!/bin/sh
  
# Restrict watch to operator namespace.
KOPF_NAMESPACED=false

# Do not attempt to coordinate with other kopf operators.
KOPF_STANDALONE=true
