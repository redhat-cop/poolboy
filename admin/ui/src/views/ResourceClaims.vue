<template>
  <h1>ResourceClaims</h1>
  <p>{{ error }}</p>
  <table>
    <thead>
      <tr>
        <th>Namespace</th>
        <th>Name</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="resourceclaim in resourceclaims" v-bind:key="resourceclaim.metadata.uid">
        <td>{{resourceclaim.metadata.namespace}}</td>
        <td><router-link :to="'/resourceclaim/' + resourceclaim.metadata.namespace + '/' + resourceclaim.metadata.name">{{resourceclaim.metadata.name}}</router-link></td>
      </tr>
    </tbody>
  </table>
</template>

<script>
export default {
  name: 'ResourceClaims',
  data () {
    return {
      error: '',
      resourceclaims: []
    }
  },
  created () {
    fetch('/session')
    .then(response => response.json())
    .then(session => {
      return fetch('/apis/poolboy.gpte.redhat.com/v1/resourceclaims', {
        headers: {
          'Authentication': 'Bearer ' + session.token
        }
      }); 
    })
    .then(response => {
      if (response.status === 200) {
        response.json().then(data => {
          this.resourceclaims = data.items
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
