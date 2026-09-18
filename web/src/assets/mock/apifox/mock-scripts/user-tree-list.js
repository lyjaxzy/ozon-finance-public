// Migrated from Easy-Mock: /geeker/user/tree/list
var MockJs = require('mockjs');

function getBody() {
  try {
    var b = $$.mockRequest.body.toJSON();
    if (b && typeof b === 'object') return b;
  } catch (e) {}
  try {
    return JSON.parse($$.mockRequest.body.toString() || '{}');
  } catch (e2) {
    return {};
  }
}

var query = getBody();
if (query.pageNum == null) query.pageNum = $$.mockRequest.getParam('pageNum');
if (query.pageSize == null) query.pageSize = $$.mockRequest.getParam('pageSize');
if (query.username == null) query.username = $$.mockRequest.getParam('username');
if (query.gender == null) query.gender = $$.mockRequest.getParam('gender');
if (query.age == null) query.age = $$.mockRequest.getParam('age');
if (query.idCard == null) query.idCard = $$.mockRequest.getParam('idCard');
if (query.email == null) query.email = $$.mockRequest.getParam('email');
if (query.status === undefined) {
  var statusParam = $$.mockRequest.getParam('status');
  if (statusParam !== undefined && statusParam !== null && statusParam !== '') query.status = statusParam;
}

var AVATARS = [
  'https://i.imgtg.com/2023/01/16/QRBHS.jpg',
  'https://i.imgtg.com/2023/01/16/QRqMK.jpg',
  'https://i.imgtg.com/2023/01/16/QR57a.jpg',
  'https://i.imgtg.com/2023/01/16/QRa0s.jpg'
];

function buildItem() {
  return {
    id: '@string(number,18)',
    username: query.username ? query.username : '@cname',
    gender: query.gender ? query.gender : '@integer(1, 2)',
    user: { detail: { age: query.age ? query.age : '@integer(10,30)' } },
    idCard: query.idCard ? query.idCard : '@id',
    email: query.email ? query.email : '@email',
    address: '@city(true)',
    createTime: '@date @time',
    status: query.status !== undefined ? query.status : '@integer(0, 1)',
    'avatar|1': AVATARS,
    'children|3': [
      {
        id: '@string(number,18)',
        username: query.username ? query.username : '@cname',
        gender: query.gender ? query.gender : '@integer(1, 2)',
        user: { detail: { age: query.age ? query.age : '@integer(10,30)' } },
        idCard: query.idCard ? query.idCard : '@id',
        email: query.email ? query.email : '@email',
        address: '@city(true)',
        createTime: '@date @time',
        status: query.status !== undefined ? query.status : '@integer(0, 1)',
        'avatar|1': AVATARS
      }
    ]
  };
}

var data;
if (query.username || query.gender || query.age || query.idCard || query.email || query.status !== undefined) {
  data = MockJs.mock({
    'list|10': [buildItem()],
    pageNum: Number(query.pageNum),
    pageSize: Number(query.pageSize),
    total: 18
  });
} else if (query.pageSize == 25) {
  data = MockJs.mock({
    'list|25': [buildItem()],
    pageNum: Number(query.pageNum),
    pageSize: Number(query.pageSize),
    total: 2000
  });
} else if (query.pageSize == 50) {
  data = MockJs.mock({
    'list|50': [buildItem()],
    pageNum: Number(query.pageNum),
    pageSize: Number(query.pageSize),
    total: 2000
  });
} else if (query.pageSize == 100) {
  data = MockJs.mock({
    'list|100': [buildItem()],
    pageNum: Number(query.pageNum),
    pageSize: Number(query.pageSize),
    total: 2000
  });
} else {
  data = MockJs.mock({
    'list|10': [buildItem()],
    pageNum: Number(query.pageNum),
    pageSize: Number(query.pageSize),
    total: 2000
  });
}

$$.mockResponse.setBody({ code: 200, data: data, msg: '成功' });
$$.mockResponse.setCode(200);
