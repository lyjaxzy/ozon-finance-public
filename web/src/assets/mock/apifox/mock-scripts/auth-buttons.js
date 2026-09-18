// Migrated from Easy-Mock: /geeker/auth/buttons
function getToken() {
  var token = $$.mockRequest.headers.get('x-access-token');
  if (!token) token = $$.mockRequest.headers.get('X-Access-Token');
  if (!token) token = $$.mockRequest.getParam('x-access-token');
  return token;
}

var token = getToken();
var data;
if (token === 'bqddxxwqmfncffacvbpkuxvwvqrhln') {
  data = {
    useProTable: ['add', 'batchAdd', 'export', 'batchDelete', 'status'],
    authButton: ['add', 'edit', 'delete', 'import', 'export']
  };
} else if (token === 'unufvdotdqxuzfbdygovfmsbftlvbn') {
  data = {
    useProTable: ['add', 'batchDelete'],
    authButton: ['add', 'edit', 'delete', 'import', 'export']
  };
}

$$.mockResponse.setBody({ code: 200, data: data, msg: '成功' });
$$.mockResponse.setCode(200);
