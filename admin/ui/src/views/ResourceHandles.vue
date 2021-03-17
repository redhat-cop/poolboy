<template>
  <h1>ResourceHandles</h1>
  <p>{{ error }}</p>
  <table>
    <thead>
      <tr>
        <th>Namespace</th>
        <th>Name</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="resourcehandle in resourcehandles" v-bind:key="resourcehandle.metadata.uid">
        <td>{{resourcehandle.metadata.namespace}}</td>
        <td><router-link :to="'/resourcehandle/' + resourcehandle.metadata.namespace + '/' + resourcehandle.metadata.name">{{resourcehandle.metadata.name}}</router-link></td>
      </tr>
    </tbody>
  </table>
</template>

<script>
export default {
  name: 'ResourceHandles',
  data () {
    return {
      error: '',
      resourcehandles: []
    }
  },
  created () {
    fetch('/session')
    .then(response => response.json())
    .then(session => {
      return fetch('/apis/poolboy.gpte.redhat.com/v1/resourcehandles', {
        headers: {
          'Authentication': 'Bearer ' + session.token
        }
      }); 
    })
    .then(response => {
      if (response.status === 200) {
        response.json().then(data => {
          this.resourcehandles = data.items
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
