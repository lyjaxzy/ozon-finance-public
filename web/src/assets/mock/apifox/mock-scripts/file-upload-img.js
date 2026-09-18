// Migrated from Easy-Mock: /geeker/file/upload/img
var MockJs = require('mockjs');
$$.mockResponse.setBody({
  code: 200,
  data: MockJs.mock({
    'fileUrl|1': ["https://i.imgtg.com/2023/01/16/QRBHS.jpg","https://i.imgtg.com/2023/01/16/QRqMK.jpg","https://i.imgtg.com/2023/01/16/QR57a.jpg","https://i.imgtg.com/2023/01/16/QRa0s.jpg"]
  }),
  msg: '成功'
});
$$.mockResponse.setCode(200);
