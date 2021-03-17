<template>
  <h1>Create ResourcePool from ResourceHandle {{$route.params.name}}</h1>
  <p>{{ error }}</p>
  <YamlTextarea :obj='resourcepool'/>
</template>

<script>
import YamlTextarea from '@/components/YamlTextarea.vue'

export default {
  name: 'ResourcePoolCreateFromHandle',
  components: {
    YamlTextarea
  },
  data () {
    return {
      error: '',
      resourcepool: ''
    }
  },
  created () {
    fetch('/session')
    .then(response => response.json())
    .then(session => {
      return fetch('/apis/poolboy.gpte.redhat.com/v1/namespaces/' + this.$route.params.namespace + '/resourcehandles/' + this.$route.params.name, {
        headers: {
          'Authentication': 'Bearer ' + session.token
        }
      }); 
    })
    .then(response => {
      if (response.status === 200) {
        response.json().then(data => {
          let resourcepool = {
            apiVersion: data.apiVersion,
            kind: 'ResourcePool',
            metadata: {
              namespace: data.metadata.namespace,
              name: '<NAME>'
            },
            spec: {
              minAvailable: 1,
              resources: data.spec.resources
            }
          };
          delete resourcepool.spec.resourceClaim;
          delete resourcepool.spec.resourcePool;
          resourcepool.spec.resources.forEach(item => {
            delete item.reference
          })
          this.resourcepool = resourcepool;
        })
      } else if(response.status === 401) {
        this.error = 'Session expired, please refresh.'
      } else if(response.status === 403) {
        this.error = 'Sorry, it seems you do not have access.'
      } else {
        this.error = response.status
      }
    })
    .catch(error => {
      this.error = error
    })
  },
}
</script>
