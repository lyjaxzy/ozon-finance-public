// Migrated from Easy-Mock: /geeker/login
// password is MD5 of "123456": e10adc3949ba59abbe56e057f20f883e
var MockJs = require('mockjs');

// Apifox Mock 优先用 getParam（会从 body/query/path 取值）
var username = $$.mockRequest.getParam('username');
var password = $$.mockRequest.getParam('password');

// 兜底：部分环境下再从 body 解析
if (username == null || password == null) {
  var body = {};
  try {
    body = $$.mockRequest.body.toJSON() || {};
  } catch (e) {
    try {
      body = JSON.parse($$.mockRequest.body.toString() || '{}');
    } catch (e2) {
      body = {};
    }
  }
  if (username == null) username = body.username;
  if (password == null) password = body.password;
}

var PWD = 'e10adc3949ba59abbe56e057f20f883e';
var okAdmin = username === 'admin' && password === PWD;
var okUser = username === 'user' && password === PWD;

if (okAdmin) {
  $$.mockResponse.setBody({
    code: 200,
    data: { access_token: 'bqddxxwqmfncffacvbpkuxvwvqrhln' },
    msg: '成功'
  });
} else if (okUser) {
  $$.mockResponse.setBody({
    code: 200,
    data: { access_token: 'unufvdotdqxuzfbdygovfmsbftlvbn' },
    msg: '成功'
  });
} else {
  $$.mockResponse.setBody({
    code: 500,
    data: null,
    msg: '用户名或密码错误'
  });
}
$$.mockResponse.setCode(200);
