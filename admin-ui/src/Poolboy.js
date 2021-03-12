let sessionToken = null

export async function maintainSession() {
  const response = await fetch('/session');
  const responseJson = await response.json();
  sessionToken = responseJson.token;
  // Refresh session token when it will expire in 30 seconds
  setTimeout(maintainSession, responseJson.lifetime * 1000 - 30)
}

export async function checkAccess() {
  const response = await fetch(
    '/apis/poolboy.gpte.redhat.com/v1/namespaces/poolboy/resourcepools',
    {
      headers: {
        Authentication: 'Bearer ' + sessionToken
      }
    }
  );
  const myJson = await response.json();
  alert(myJson.kind)
}
