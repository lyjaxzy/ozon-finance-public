// Migrated from Easy-Mock: /geeker/account/list
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
if (query.idCard == null) query.idCard = $$.mockRequest.getParam('idCard');
if (query.email == null) query.email = $$.mockRequest.getParam('email');
if (query.status === undefined) {
  var statusParam = $$.mockRequest.getParam('status');
  if (statusParam !== undefined && statusParam !== null && statusParam !== '') query.status = statusParam;
}

var AVATARS = [
  'https://iamge-1259297738.cos.ap-chengdu.myqcloud.com/img/20220728110013.jpg',
  'https://iamge-1259297738.cos.ap-chengdu.myqcloud.com/img/20220728110015.jpg',
  'https://iamge-1259297738.cos.ap-chengdu.myqcloud.com/img/20220728110012.jpg',
  'https://iamge-1259297738.cos.ap-chengdu.myqcloud.com/img/20220728110032.jpg'
];

function buildItem() {
  return {
    id: '@string(number,20)',
    username: query.username ? query.username : '@cname',
    gender: query.gender ? query.gender : '@integer(1, 2)',
    idCard: query.idCard ? query.idCard : '@id',
    email: query.email ? query.email : '@email',
    address: '@city(true)',
    createTime: '@date @time',
    status: query.status !== undefined ? query.status : '@integer(0, 1)',
    'avatar|1': AVATARS
  };
}

var data;
if (query.pageSize > 10) {
  data = MockJs.mock({
    'datalist|18': [buildItem()],
    pageNum: Number(query.pageNum),
    pageSize: Number(query.pageSize),
    total: 18
  });
} else {
  data = MockJs.mock({
    'datalist|10': [buildItem()],
    pageNum: Number(query.pageNum),
    pageSize: Number(query.pageSize),
    total: 18
  });
}

$$.mockResponse.setBody({ code: 200, data: data, msg: '成功' });
$$.mockResponse.setCode(200);
