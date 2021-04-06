import { createApp } from 'vue'
import App from './App.vue'
import router from './router'

function getApiSession() {
  return fetch('/session')
  .then(response => response.json())
  .then(session => {
    setTimeout(getApiSession, (session.lifetime - 60) * 1000 );
    window.apiSession = new Promise((resolve) => resolve(session))
  })
}

window.apiSession = new Promise((resolve) => {
  getApiSession().then(session => resolve(session))
})

createApp(App).use(router).mount('#app')
