export const name = "dsh-stonebot-artificial-stone-layout";

// This package intentionally registers no host-side tools of its own.
// cordis.patch.yml mounts the built-in DSH MCP client, which discovers the
// canonical Stonebot tool contract from the Python server.
export function apply() {}
