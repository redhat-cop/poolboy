module.exports = {
  devServer: {
    proxy: {
      '/apis': {
        target: 'http://localhost:5000/'
      },
      '/session': {
        target: 'http://localhost:5000/'
      }
    }
  }
}
