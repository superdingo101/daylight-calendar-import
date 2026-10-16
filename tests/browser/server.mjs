// Serve only the shipped ES modules and local harness; no Home Assistant session.
import {createServer} from "node:http";
import {createHash} from "node:crypto";
import {readFile} from "node:fs/promises";
import {fileURLToPath} from "node:url";

const frontend = new URL("../../custom_components/daylight_calendar_import/frontend/", import.meta.url);
createServer(async (request, response) => {
  const path = new URL(request.url, "http://localhost").pathname;
  if (path === "/api/file_upload") {
    // Inspect the multipart body received over HTTP, including actual File bytes.
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const multipart = Buffer.concat(chunks);
    const boundary = request.headers["content-type"]?.match(/boundary=(.+)$/)?.[1];
    const headerEnd = multipart.indexOf("\r\n\r\n");
    const fileEnd = multipart.indexOf(`\r\n--${boundary}`, headerEnd + 4);
    const file = multipart.subarray(headerEnd + 4, fileEnd);
    response.writeHead(200, {"Content-Type": "application/json"});
    response.end(JSON.stringify(request.method === "POST" ? {
      file_id: "a".repeat(32),
      observed: {headers: multipart.subarray(0, headerEnd).toString("utf8"),
        size: file.length, sha256: createHash("sha256").update(file).digest("hex")},
    } : {}));
    return;
  }
  const filename = path === "/" ? new URL("harness.html", import.meta.url) :
    /^\/frontend\/[a-z_]+\.js$/.test(path) ? new URL(path.split("/").pop(), frontend) : null;
  if (!filename) { response.writeHead(404).end(); return; }
  try {
    const body = await readFile(fileURLToPath(filename));
    response.writeHead(200, {"Content-Type": path === "/" ? "text/html" : "text/javascript"});
    response.end(body);
  } catch { response.writeHead(404).end(); }
}).listen(4179, "127.0.0.1");
