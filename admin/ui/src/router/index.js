import { createRouter, createWebHistory } from 'vue-router'
import Home from '../views/Home.vue'

const routes = [
  {
    path: '/',
    name: 'Home',
    component: Home
  },{
    path: '/resourcepools',
    name: 'ResourcePools',
    component: () => import('../views/ResourcePools.vue')
  },{
    path: '/resourcepool/:namespace/:name',
    name: 'ResourcePool',
    component: () => import('../views/ResourcePool.vue')
  },{
    path: '/resourcepool/createfrom/handle/:namespace/:name',
    name: 'ResourcePoolFromHandle',
    component: () => import('../views/ResourcePoolCreateFromHandle.vue')
  },{
    path: '/resourceclaims',
    name: 'ResourceClaims',
    component: () => import('../views/ResourceClaims.vue')
  },{
    path: '/resourceclaim/:namespace/:name',
    name: 'ResourceClaim',
    component: () => import('../views/ResourceClaim.vue')
  },{
    path: '/resourcehandles',
    name: 'ResourceHandles',
    component: () => import('../views/ResourceHandles.vue')
  },{
    path: '/resourcehandle/:namespace/:name',
    name: 'ResourceHandle',
    component: () => import('../views/ResourceHandle.vue')
  },{
    path: '/resourceprovider/:namespace/:name',
    name: 'ResourceProvider',
    component: () => import('../views/ResourceProvider.vue')
  },{
    path: '/resourceproviders',
    name: 'ResourceProviders',
    component: () => import('../views/ResourceProviders.vue')
  }
]

const router = createRouter({
  history: createWebHistory(process.env.BASE_URL),
  routes
})

export default router
