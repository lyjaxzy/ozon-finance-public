/**
 * Convert Easy-Mock geeker APIs -> Apifox OpenAPI 3.0 + Mock Scripts
 * Logic preserved from src/assets/mock/geeker/**
 */
import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const GEEKER = path.join(ROOT, 'geeker');
const OUT = __dirname;

const ADMIN_TOKEN = 'bqddxxwqmfncffacvbpkuxvwvqrhln';
const USER_TOKEN = 'unufvdotdqxuzfbdygovfmsbftlvbn';
const PWD_MD5 = 'e10adc3949ba59abbe56e057f20f883e'; // 123456

function loadEasyMock(rel) {
  let text = fs.readFileSync(path.join(GEEKER, rel), 'utf8').trim();
  if (text.endsWith(',')) text = text.slice(0, -1);
  return vm.runInNewContext(`(${text})`, {}, { timeout: 5000 });
}

function evalStatic(rel, req = { body: {}, header: {} }) {
  const mock = loadEasyMock(rel);
  const Mock = { mock: (x) => x };
  const out = {};
  for (const [k, v] of Object.entries(mock)) {
    out[k] = typeof v === 'function' ? v({ _req: req, Mock }) : v;
  }
  return out;
}

const menuMock = loadEasyMock('menu/list.json');
const buttonsMock = loadEasyMock('auth/buttons.json');
const menuAdmin = menuMock.data({
  _req: { header: { 'x-access-token': ADMIN_TOKEN }, body: {} },
  Mock: { mock: (x) => x },
});
const menuUser = menuMock.data({
  _req: { header: { 'x-access-token': USER_TOKEN }, body: {} },
  Mock: { mock: (x) => x },
});
const buttonsAdmin = buttonsMock.data({
  _req: { header: { 'x-access-token': ADMIN_TOKEN } },
});
const buttonsUser = buttonsMock.data({
  _req: { header: { 'x-access-token': USER_TOKEN } },
});

const staticResponses = {
  logout: evalStatic('logout.json'),
  user_add: evalStatic('user/add.json'),
  user_edit: evalStatic('user/edit.json'),
  user_delete: evalStatic('user/delete.json'),
  user_change: evalStatic('user/change.json'),
  user_import: evalStatic('user/import.json'),
  user_export: evalStatic('user/export.json'),
  user_rest_password: evalStatic('user/rest_password.json'),
  user_status: evalStatic('user/status.json'),
  user_gender: evalStatic('user/gender.json'),
  user_department: evalStatic('user/department.json'),
  user_role: evalStatic('user/role.json'),
  file_upload_video: evalStatic('file/upload/video.json'),
};

const AVATARS = [
  'https://i.imgtg.com/2023/01/16/QRBHS.jpg',
  'https://i.imgtg.com/2023/01/16/QRqMK.jpg',
  'https://i.imgtg.com/2023/01/16/QR57a.jpg',
  'https://i.imgtg.com/2023/01/16/QRa0s.jpg',
];
const ACCOUNT_AVATARS = [
  'https://iamge-1259297738.cos.ap-chengdu.myqcloud.com/img/20220728110013.jpg',
  'https://iamge-1259297738.cos.ap-chengdu.myqcloud.com/img/20220728110015.jpg',
  'https://iamge-1259297738.cos.ap-chengdu.myqcloud.com/img/20220728110012.jpg',
  'https://iamge-1259297738.cos.ap-chengdu.myqcloud.com/img/20220728110032.jpg',
];

/** Build Apifox mock script that mirrors Easy-Mock function fields */
function scriptFromEasyMockObject(objLiteralSource) {
  // We hand-write scripts for clarity; this helper unused for complex ones
  return objLiteralSource;
}

// ---------- Mock Scripts (logic identical to Easy-Mock) ----------
const scripts = {};

scripts['login'] = `// Migrated from Easy-Mock: /geeker/login
// password is MD5 of "123456": ${PWD_MD5}
var MockJs = require('mockjs');
var body = $$.mockRequest.body.toJSON() || {};
var username = body.username;
var password = body.password;
var okAdmin = username === 'admin' && password === '${PWD_MD5}';
var okUser = username === 'user' && password === '${PWD_MD5}';

if (okAdmin) {
  $$.mockResponse.setBody({
    code: 200,
    data: MockJs.mock({ access_token: '${ADMIN_TOKEN}' }),
    msg: '成功'
  });
} else if (okUser) {
  $$.mockResponse.setBody({
    code: 200,
    data: MockJs.mock({ access_token: '${USER_TOKEN}' }),
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
`;

scripts['auth-buttons'] = `// Migrated from Easy-Mock: /geeker/auth/buttons
var token = $$.mockRequest.headers.get('x-access-token');
var data;
if (token === '${ADMIN_TOKEN}') {
  data = ${JSON.stringify(buttonsAdmin, null, 2)};
} else if (token === '${USER_TOKEN}') {
  data = ${JSON.stringify(buttonsUser, null, 2)};
}
$$.mockResponse.setBody({ code: 200, data: data, msg: '成功' });
$$.mockResponse.setCode(200);
`;

scripts['menu-list'] = `// Migrated from Easy-Mock: /geeker/menu/list
var token = $$.mockRequest.headers.get('x-access-token');
var data;
if (token === '${ADMIN_TOKEN}') {
  data = ${JSON.stringify(menuAdmin)};
} else if (token === '${USER_TOKEN}') {
  data = ${JSON.stringify(menuUser)};
}
$$.mockResponse.setBody({ code: 200, data: data, msg: '成功' });
$$.mockResponse.setCode(200);
`;

scripts['file-upload-img'] = `// Migrated from Easy-Mock: /geeker/file/upload/img
var MockJs = require('mockjs');
$$.mockResponse.setBody({
  code: 200,
  data: MockJs.mock({
    'fileUrl|1': ${JSON.stringify(AVATARS)}
  }),
  msg: '成功'
});
$$.mockResponse.setCode(200);
`;

function buildUserListItemTemplate(withChildren) {
  const children = withChildren
    ? `,
          'children|3': [{
            "id": "@string(number,18)",
            "username": query.username ? query.username : "@cname",
            "gender": query.gender ? query.gender : "@integer(1, 2)",
            "user": { "detail": { "age": query.age ? query.age : "@integer(10,30)" } },
            "idCard": query.idCard ? query.idCard : "@id",
            "email": query.email ? query.email : "@email",
            "address": "@city(true)",
            "createTime": "@date @time",
            "status": query.status !== undefined ? query.status : "@integer(0, 1)",
            "avatar|1": ${JSON.stringify(AVATARS)}
          }]`
    : '';
  return `{
          "id": "@string(number,18)",
          "username": query.username ? query.username : "@cname",
          "gender": query.gender ? query.gender : "@integer(1, 2)",
          "user": { "detail": { "age": query.age ? query.age : "@integer(10,30)" } },
          "idCard": query.idCard ? query.idCard : "@id",
          "email": query.email ? query.email : "@email",
          "address": "@city(true)",
          "createTime": "@date @time",
          "status": query.status !== undefined ? query.status : "@integer(0, 1)",
          "avatar|1": ${JSON.stringify(AVATARS)}${children}
        }`;
}

scripts['user-list'] = `// Migrated from Easy-Mock: /geeker/user/list
var MockJs = require('mockjs');
var query = $$.mockRequest.body.toJSON() || {};
var item = ${buildUserListItemTemplate(false)};
var data;
if (query.username || query.gender || query.age || query.idCard || query.email || query.status !== undefined) {
  data = MockJs.mock({
    "list|10": [item],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 18
  });
} else if (query.pageSize == 25) {
  data = MockJs.mock({
    "list|25": [item],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 2000
  });
} else if (query.pageSize == 50) {
  data = MockJs.mock({
    "list|50": [item],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 2000
  });
} else if (query.pageSize == 100) {
  data = MockJs.mock({
    "list|100": [item],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 2000
  });
} else {
  data = MockJs.mock({
    "list|10": [item],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 2000
  });
}
$$.mockResponse.setBody({ code: 200, data: data, msg: '成功' });
$$.mockResponse.setCode(200);
`;

scripts['user-tree-list'] = `// Migrated from Easy-Mock: /geeker/user/tree/list
var MockJs = require('mockjs');
var query = $$.mockRequest.body.toJSON() || {};
var item = ${buildUserListItemTemplate(true)};
var data;
if (query.username || query.gender || query.age || query.idCard || query.email || query.status !== undefined) {
  data = MockJs.mock({
    "list|10": [item],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 18
  });
} else if (query.pageSize == 25) {
  data = MockJs.mock({
    "list|25": [item],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 2000
  });
} else if (query.pageSize == 50) {
  data = MockJs.mock({
    "list|50": [item],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 2000
  });
} else if (query.pageSize == 100) {
  data = MockJs.mock({
    "list|100": [item],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 2000
  });
} else {
  data = MockJs.mock({
    "list|10": [item],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 2000
  });
}
$$.mockResponse.setBody({ code: 200, data: data, msg: '成功' });
$$.mockResponse.setCode(200);
`;

scripts['account-list'] = `// Migrated from Easy-Mock: /geeker/account/list
var MockJs = require('mockjs');
var query = $$.mockRequest.body.toJSON() || {};
var itemTpl = {
  "id": "@string(number,20)",
  "username": query.username ? query.username : "@cname",
  "gender": query.gender ? query.gender : "@integer(1, 2)",
  "idCard": query.idCard ? query.idCard : "@id",
  "email": query.email ? query.email : "@email",
  "address": "@city(true)",
  "createTime": "@date @time",
  "status": query.status !== undefined ? query.status : "@integer(0, 1)",
  "avatar|1": ${JSON.stringify(ACCOUNT_AVATARS)}
};
var data;
if (query.pageSize > 10) {
  data = MockJs.mock({
    "datalist|18": [itemTpl],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 18
  });
} else {
  data = MockJs.mock({
    "datalist|10": [itemTpl],
    "pageNum": Number(query.pageNum),
    "pageSize": Number(query.pageSize),
    "total": 18
  });
}
$$.mockResponse.setBody({ code: 200, data: data, msg: '成功' });
$$.mockResponse.setCode(200);
`;

// Write scripts
const scriptDir = path.join(OUT, 'mock-scripts');
fs.mkdirSync(scriptDir, { recursive: true });
for (const [name, content] of Object.entries(scripts)) {
  fs.writeFileSync(path.join(scriptDir, `${name}.js`), content);
}

// ---------- OpenAPI ----------
const ResultSchema = {
  type: 'object',
  properties: {
    code: { type: 'integer', example: 200 },
    data: {},
    msg: { type: 'string', example: '成功' },
  },
};

function op({ summary, folder, method, requestBody, responses, scriptKey, description, tags }) {
  const o = {
    summary,
    tags: tags || [folder],
    description: description || '',
    'x-apifox-folder': folder,
    'x-apifox-status': 'released',
    responses: responses || {
      '200': {
        description: '成功',
        content: {
          'application/json': {
            schema: ResultSchema,
          },
        },
      },
    },
  };
  if (requestBody) o.requestBody = requestBody;
  if (scriptKey && scripts[scriptKey]) {
    o.description =
      (o.description ? o.description + '\n\n' : '') +
      `> Apifox 高级 Mock 脚本见 \`mock-scripts/${scriptKey}.js\`（导入后粘贴到「高级 Mock → 脚本」并开启）`;
    // Apifox 自定义扩展：部分版本可识别，无法识别时不影响导入
    o['x-apifox'] = {
      mockScript: {
        enable: true,
        script: scripts[scriptKey],
      },
    };
  }
  return o;
}

function jsonBody(example, schema) {
  return {
    required: true,
    content: {
      'application/json': {
        schema: schema || { type: 'object' },
        ...(example !== undefined ? { example } : {}),
      },
    },
  };
}

function jsonResp(example, examples) {
  const content = {
    'application/json': {
      schema: ResultSchema,
    },
  };
  if (examples) content['application/json'].examples = examples;
  else if (example !== undefined) content['application/json'].example = example;
  return {
    '200': {
      description: '成功',
      content,
      'x-apifox-name': '成功',
    },
  };
}

const paths = {
  '/geeker/login': {
    post: op({
      summary: '用户登录',
      folder: '认证',
      scriptKey: 'login',
      requestBody: jsonBody(
        { username: 'admin', password: PWD_MD5 },
        {
          type: 'object',
          required: ['username', 'password'],
          properties: {
            username: { type: 'string' },
            password: { type: 'string', description: 'MD5(明文密码)，演示密码 123456 的 MD5' },
          },
        }
      ),
      responses: jsonResp(undefined, {
        adminSuccess: {
          summary: 'admin 登录成功',
          value: { code: 200, data: { access_token: ADMIN_TOKEN }, msg: '成功' },
        },
        userSuccess: {
          summary: 'user 登录成功',
          value: { code: 200, data: { access_token: USER_TOKEN }, msg: '成功' },
        },
        fail: {
          summary: '用户名或密码错误',
          value: { code: 500, data: null, msg: '用户名或密码错误' },
        },
      }),
    }),
  },
  '/geeker/logout': {
    post: op({
      summary: '退出登录',
      folder: '认证',
      responses: jsonResp(staticResponses.logout),
    }),
  },
  '/geeker/menu/list': {
    get: op({
      summary: '获取菜单列表',
      folder: '认证',
      scriptKey: 'menu-list',
      description: '根据请求头 x-access-token 返回不同菜单（admin / user）',
      responses: jsonResp(undefined, {
        admin: {
          summary: 'admin 菜单',
          value: { code: 200, data: menuAdmin, msg: '成功' },
        },
        user: {
          summary: 'user 菜单',
          value: { code: 200, data: menuUser, msg: '成功' },
        },
      }),
    }),
  },
  '/geeker/auth/buttons': {
    get: op({
      summary: '获取按钮权限',
      folder: '认证',
      scriptKey: 'auth-buttons',
      description: '根据请求头 x-access-token 返回不同按钮权限',
      responses: jsonResp(undefined, {
        admin: {
          summary: 'admin 按钮权限',
          value: { code: 200, data: buttonsAdmin, msg: '成功' },
        },
        user: {
          summary: 'user 按钮权限',
          value: { code: 200, data: buttonsUser, msg: '成功' },
        },
      }),
    }),
  },
  '/geeker/user/list': {
    post: op({
      summary: '获取用户列表',
      folder: '用户管理',
      scriptKey: 'user-list',
      requestBody: jsonBody({
        pageNum: 1,
        pageSize: 10,
        username: '',
        gender: undefined,
        idCard: '',
        email: '',
        status: undefined,
      }),
      responses: jsonResp({
        code: 200,
        data: { list: [], pageNum: 1, pageSize: 10, total: 2000 },
        msg: '成功',
      }),
    }),
  },
  '/geeker/user/tree/list': {
    post: op({
      summary: '获取树形用户列表',
      folder: '用户管理',
      scriptKey: 'user-tree-list',
      requestBody: jsonBody({ pageNum: 1, pageSize: 10 }),
      responses: jsonResp({
        code: 200,
        data: { list: [], pageNum: 1, pageSize: 10, total: 2000 },
        msg: '成功',
      }),
    }),
  },
  '/geeker/user/add': {
    post: op({
      summary: '新增用户',
      folder: '用户管理',
      requestBody: jsonBody({ id: '1' }),
      responses: jsonResp(staticResponses.user_add),
    }),
  },
  '/geeker/user/edit': {
    post: op({
      summary: '编辑用户',
      folder: '用户管理',
      requestBody: jsonBody({}),
      responses: jsonResp(staticResponses.user_edit),
    }),
  },
  '/geeker/user/delete': {
    post: op({
      summary: '删除用户',
      folder: '用户管理',
      requestBody: jsonBody({}),
      responses: jsonResp(staticResponses.user_delete),
    }),
  },
  '/geeker/user/change': {
    post: op({
      summary: '切换用户状态',
      folder: '用户管理',
      requestBody: jsonBody({}),
      responses: jsonResp(staticResponses.user_change),
    }),
  },
  '/geeker/user/import': {
    post: op({
      summary: '导入用户',
      folder: '用户管理',
      requestBody: {
        required: true,
        content: {
          'multipart/form-data': {
            schema: { type: 'object', properties: { file: { type: 'string', format: 'binary' } } },
          },
        },
      },
      responses: jsonResp(staticResponses.user_import),
    }),
  },
  '/geeker/user/export': {
    post: op({
      summary: '导出用户',
      folder: '用户管理',
      requestBody: jsonBody({}),
      responses: jsonResp(staticResponses.user_export),
    }),
  },
  '/geeker/user/rest_password': {
    post: op({
      summary: '重置密码',
      folder: '用户管理',
      requestBody: jsonBody({}),
      responses: jsonResp(staticResponses.user_rest_password),
    }),
  },
  '/geeker/user/status': {
    get: op({
      summary: '用户状态字典',
      folder: '用户管理',
      responses: jsonResp(staticResponses.user_status),
    }),
  },
  '/geeker/user/gender': {
    get: op({
      summary: '用户性别字典',
      folder: '用户管理',
      responses: jsonResp(staticResponses.user_gender),
    }),
  },
  '/geeker/user/department': {
    get: op({
      summary: '部门树',
      folder: '用户管理',
      responses: jsonResp(staticResponses.user_department),
    }),
  },
  '/geeker/user/role': {
    get: op({
      summary: '角色列表',
      folder: '用户管理',
      responses: jsonResp(staticResponses.user_role),
    }),
  },
  '/geeker/account/list': {
    post: op({
      summary: '账号列表',
      folder: '账号管理',
      scriptKey: 'account-list',
      requestBody: jsonBody({ pageNum: 1, pageSize: 10 }),
      responses: jsonResp({
        code: 200,
        data: { datalist: [], pageNum: 1, pageSize: 10, total: 18 },
        msg: '成功',
      }),
    }),
  },
  '/geeker/file/upload/img': {
    post: op({
      summary: '上传图片',
      folder: '文件上传',
      scriptKey: 'file-upload-img',
      requestBody: {
        required: true,
        content: {
          'multipart/form-data': {
            schema: { type: 'object', properties: { file: { type: 'string', format: 'binary' } } },
          },
        },
      },
      responses: jsonResp({
        code: 200,
        data: { fileUrl: AVATARS[0] },
        msg: '成功',
      }),
    }),
  },
  '/geeker/file/upload/video': {
    post: op({
      summary: '上传视频',
      folder: '文件上传',
      requestBody: {
        required: true,
        content: {
          'multipart/form-data': {
            schema: { type: 'object', properties: { file: { type: 'string', format: 'binary' } } },
          },
        },
      },
      responses: jsonResp(staticResponses.file_upload_video),
    }),
  },
};

// Add security header note for token-based APIs
for (const p of ['/geeker/menu/list', '/geeker/auth/buttons']) {
  paths[p].get.parameters = [
    {
      name: 'x-access-token',
      in: 'header',
      required: false,
      schema: { type: 'string' },
      description: `admin: ${ADMIN_TOKEN} | user: ${USER_TOKEN}`,
      example: ADMIN_TOKEN,
    },
  ];
}

const openapi = {
  openapi: '3.0.3',
  info: {
    title: 'Geeker-Admin Mock API',
    description: [
      '从 Easy-Mock（geeker）迁移到 Apifox。',
      '',
      '## 导入说明',
      '1. Apifox → 项目设置 → 导入数据 → OpenAPI/Swagger → 选择本文件 `geeker.openapi.json`',
      '2. 建议开启：导入 Servers 为环境；冲突处理选「覆盖已有接口」或「智能合并」',
      '3. 有动态逻辑的接口：打开「高级 Mock → 脚本」，粘贴同目录 `mock-scripts/*.js` 内容并开启开关',
      '',
      '## 账号',
      `- admin / 123456（body.password 为 MD5: ${PWD_MD5}）→ token: ${ADMIN_TOKEN}`,
      `- user / 123456（同上）→ token: ${USER_TOKEN}`,
      '',
      '## 代理',
      '原 Easy-Mock 基址：`https://mock.mengxuegu.com/mock/629d727e6163854a32e8307e`',
      '导入后把 `.env.development` 的 `VITE_PROXY` 改为 Apifox Mock 地址即可。',
    ].join('\n'),
    version: '1.0.0',
  },
  servers: [
    {
      url: 'https://mock.apifox.cn/m1/{projectId}',
      description: 'Apifox 云端 Mock（导入后替换 projectId）',
      variables: { projectId: { default: 'YOUR_PROJECT_ID' } },
    },
    {
      url: 'http://127.0.0.1:4523/m1/{projectId}',
      description: 'Apifox 本地 Mock',
      variables: { projectId: { default: 'YOUR_PROJECT_ID' } },
    },
  ],
  tags: [
    { name: '认证' },
    { name: '用户管理' },
    { name: '账号管理' },
    { name: '文件上传' },
  ],
  paths,
  components: {
    securitySchemes: {
      AccessToken: {
        type: 'apiKey',
        in: 'header',
        name: 'x-access-token',
        description: '登录接口返回的 access_token',
      },
    },
    schemas: {
      Result: ResultSchema,
    },
  },
};

const openapiPath = path.join(OUT, 'geeker.openapi.json');
fs.writeFileSync(openapiPath, JSON.stringify(openapi, null, 2));

// Also write a expectations helper JSON for login/menu/buttons (optional manual create)
const expectations = {
  _comment:
    '可选：在 Apifox「高级 Mock → 期望」中按条件创建。动态列表类接口请优先使用 mock-scripts。',
  login: [
    {
      name: 'admin 成功',
      conditions: [
        { type: 'body', paramName: 'username', comparator: 'eq', value: 'admin' },
        { type: 'body', paramName: 'password', comparator: 'eq', value: PWD_MD5 },
      ],
      body: { code: 200, data: { access_token: ADMIN_TOKEN }, msg: '成功' },
    },
    {
      name: 'user 成功',
      conditions: [
        { type: 'body', paramName: 'username', comparator: 'eq', value: 'user' },
        { type: 'body', paramName: 'password', comparator: 'eq', value: PWD_MD5 },
      ],
      body: { code: 200, data: { access_token: USER_TOKEN }, msg: '成功' },
    },
    {
      name: '失败',
      conditions: [],
      body: { code: 500, data: null, msg: '用户名或密码错误' },
    },
  ],
  'menu/list': [
    {
      name: 'admin 菜单',
      conditions: [{ type: 'header', paramName: 'x-access-token', comparator: 'eq', value: ADMIN_TOKEN }],
      body: { code: 200, data: menuAdmin, msg: '成功' },
    },
    {
      name: 'user 菜单',
      conditions: [{ type: 'header', paramName: 'x-access-token', comparator: 'eq', value: USER_TOKEN }],
      body: { code: 200, data: menuUser, msg: '成功' },
    },
  ],
  'auth/buttons': [
    {
      name: 'admin 按钮',
      conditions: [{ type: 'header', paramName: 'x-access-token', comparator: 'eq', value: ADMIN_TOKEN }],
      body: { code: 200, data: buttonsAdmin, msg: '成功' },
    },
    {
      name: 'user 按钮',
      conditions: [{ type: 'header', paramName: 'x-access-token', comparator: 'eq', value: USER_TOKEN }],
      body: { code: 200, data: buttonsUser, msg: '成功' },
    },
  ],
};
fs.writeFileSync(path.join(OUT, 'geeker.mock-expectations.json'), JSON.stringify(expectations, null, 2));

console.log('Generated:');
console.log(' -', openapiPath);
console.log(' -', path.join(OUT, 'geeker.mock-expectations.json'));
console.log(' - mock-scripts:', Object.keys(scripts).join(', '));
console.log('paths:', Object.keys(paths).length);
