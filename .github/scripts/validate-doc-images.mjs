// Check local image files using a real MDX parser, not regex over source text.
import { readFileSync, existsSync, readdirSync, realpathSync, statSync } from "node:fs";
import { resolve, relative, dirname, extname, isAbsolute, sep } from "node:path";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkMdx from "remark-mdx";

const docs = realpathSync(resolve(process.argv[2] ?? "docs"));
const parser = unified().use(remarkParse).use(remarkMdx);
const errors = [];
function* pages(dir) {
  for (const item of readdirSync(dir, { withFileTypes: true })) {
    const file = resolve(dir, item.name);
    if (item.isDirectory()) yield* pages(file);
    else if (item.isFile() && [".md", ".mdx"].includes(extname(file))) yield file;
  }
}
function visit(node, fn) {
  fn(node);
  for (const child of node.children ?? []) visit(child, fn);
}
function identifier(id) {
  return id.trim().replace(/\s+/g, " ").toLowerCase();
}
function check(src, page) {
  if (!src || src.startsWith("#") || src.startsWith("//") ||
      /^[a-z][a-z\d+.-]*:/i.test(src)) return;
  let path;
  try {
    path = decodeURIComponent(src.split(/[?#]/, 1)[0]);
  } catch {
    errors.push(relative(docs, page) + ": invalid image URL " + src);
    return;
  }
  if (!path) return;
  const target = path.startsWith("/") ? resolve(docs, "." + path) : resolve(dirname(page), path);
  const rel = relative(docs, target);
  if (rel === ".." || rel.startsWith(".." + sep) || isAbsolute(rel) ||
      !existsSync(target) || !statSync(target).isFile() ||
      !realpathSync(target).startsWith(docs + sep)) {
    errors.push(relative(docs, page) + ": missing or out-of-root image " + src);
  }
}
for (const page of pages(docs)) {
  const ast = parser.parse(readFileSync(page, "utf8"));
  const definitions = new Map();
  visit(ast, node => {
    if (node.type === "definition" && !definitions.has(identifier(node.identifier))) {
      definitions.set(identifier(node.identifier), node.url);
    }
  });
  visit(ast, node => {
    if (node.type === "image") check(node.url, page);
    if (node.type === "imageReference") {
      const src = definitions.get(identifier(node.identifier));
      if (src) check(src, page);
    }
    if ((node.type === "mdxJsxFlowElement" || node.type === "mdxJsxTextElement") &&
        node.name === "img") {
      for (const attr of node.attributes ?? []) {
        if (attr.type === "mdxJsxAttribute" && attr.name === "src" &&
            typeof attr.value === "string") check(attr.value, page);
      }
    }
  });
}
if (errors.length) {
  errors.forEach(error => console.error(error));
  process.exitCode = 1;
} else {
  console.log("Local Markdown and MDX images validated");
}
