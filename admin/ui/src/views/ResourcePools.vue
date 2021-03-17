<template>
  <h1>ResourcePools</h1>
  <p>{{ error }}</p>
  <table>
    <thead>
      <tr>
        <th>Namespace</th>
        <th>Name</th>
        <th>Min Available</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="resourcepool in resourcepools" v-bind:key="resourcepool.metadata.uid">
        <td>{{resourcepool.metadata.namespace}}</td>
        <td><router-link :to="'/resourcepool/' + resourcepool.metadata.namespace + '/' + resourcepool.metadata.name">{{resourcepool.metadata.name}}</router-link></td>
        <td>{{resourcepool.spec.minAvailable}}</td>
      </tr>
    </tbody>
  </table>
</template>

<script>
export default {
  name: 'ResourcePools',
  data () {
    return {
      error: '',
      resourcepools: []
    }
  },
  created () {
    fetch('/session')
    .then(response => response.json())
    .then(session => {
      return fetch('/apis/poolboy.gpte.redhat.com/v1/resourcepools', {
        headers: {
          'Authentication': 'Bearer ' + session.token
        }
      }); 
    })
    .then(response => {
      if (response.status === 200) {
        response.json().then(data => {
          this.resourcepools = data.items
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
/*
  computed: {
    resourcepools: function() {
      return this.resourcepool_data
    }
  },
*/
}
</script>
