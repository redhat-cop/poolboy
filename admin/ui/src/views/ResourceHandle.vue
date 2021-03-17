<template>
  <h1>ResourceHandle {{$route.params.name}}</h1>
  <p>{{ error }}</p>
  <p><router-link :to="'/resourcepool/createfrom/handle/' + $route.params.namespace + '/' + $route.params.name">Create Pool from Handle</router-link></p>
  <YamlBlob :obj='resourcehandle'/>
</template>

<script>
import YamlBlob from '@/components/YamlBlob.vue'

export default {
  name: 'ResourceHandle',
  components: {
    YamlBlob
  },
  data () {
    return {
      error: '',
      resourcehandle: ''
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
          this.resourcehandle = data
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
