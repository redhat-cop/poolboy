<template>
  <h1>ResourcePool {{$route.params.name}}</h1>
  <p>{{ error }}</p>
  <YamlBlob :obj='resourcepool'/>
</template>

<script>
import YamlBlob from '@/components/YamlBlob.vue'

export default {
  name: 'ResourcePool',
  components: {
    YamlBlob
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
      return fetch('/apis/poolboy.gpte.redhat.com/v1/namespaces/' + this.$route.params.namespace + '/resourcepools/' + this.$route.params.name, {
        headers: {
          'Authentication': 'Bearer ' + session.token
        }
      }); 
    })
    .then(response => {
      if (response.status === 200) {
        response.json().then(data => {
          this.resourcepool = data
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
