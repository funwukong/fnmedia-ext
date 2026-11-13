// register.mjs —— Node 20+/24 稳定入口：注册同目录 loader.mjs 的 customization hooks。
// 用法：node --import ./register.mjs sidecar.mjs
import { register } from "node:module";
import { pathToFileURL } from "node:url";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
register(pathToFileURL(path.join(HERE, "loader.mjs")).href, pathToFileURL(HERE + "/"));
