-- agent_card_rewrite.lua: rewrite A2A agent-card endpoint URLs from the backend
-- to the gateway so clients route JSON-RPC calls back through this proxy
-- Runs in the body_filter phase on the agent-card discovery route.
-- The companion header_filter clears Content-Length because the body size changes.
local ok, cjson = pcall(require, "cjson")
if not ok then return end

local chunk = ngx.arg[1]
local eof = ngx.arg[2]

-- Buffer the upstream body across chunks; emit nothing until the final chunk.
local buf = ngx.ctx.agent_card_buf
if buf == nil then
    buf = {}
    ngx.ctx.agent_card_buf = buf
end
if chunk and chunk ~= "" then
    buf[#buf + 1] = chunk
end
if not eof then
    ngx.arg[1] = nil
    return
end

local body = table.concat(buf)
ngx.ctx.agent_card_buf = nil

local dok, card = pcall(cjson.decode, body)
if not dok or type(card) ~= "table" then
    -- Not an agent card we understand; pass the original body through unchanged.
    ngx.arg[1] = body
    return
end

-- Gateway base for this agent is the request URI minus the agent-card suffix,
-- e.g. /agent/travel/.well-known/agent-card.json -> /agent/travel
local base = ngx.var.uri:gsub("/%.well%-known/agent%-card%.json$", "")
-- Prefer the client-facing scheme from X-Forwarded-Proto (set by a
-- TLS-terminating proxy like CloudFront/ALB, which forwards to nginx over plain
-- http so ngx.var.scheme would wrongly be "http"); fall back to ngx.var.scheme
-- for direct-TLS deployments (local/compose) where the header is absent. Only
-- accept the exact values http/https so a spoofed header cannot inject junk.
local xfp = ngx.var.http_x_forwarded_proto
local scheme = (xfp == "https" or xfp == "http") and xfp or ngx.var.scheme
-- http_host preserves a non-default port (e.g. :8443); ngx.var.host strips it.
-- Trailing slash so the advertised URL matches the JSON-RPC endpoint, which is
-- the prefix location {ROOT_PATH}/agent/<path>/ (a no-slash URL would not match).
local gateway_url = scheme .. "://" .. ngx.var.http_host .. base .. "/"

-- Collect the exact backend URL strings to rewrite (top-level url + any
-- advertised interface urls across A2A versions: additionalInterfaces (0.2.x)
-- and supportedInterfaces (proto/1.0)). We use the decoded card ONLY to find
-- these values, then rewrite them by literal string substitution on the raw
-- body below. We do NOT re-encode the decoded card: cjson serializes an empty
-- array (e.g. a skill's "tags": []) as "{}", which corrupts the card and makes
-- strict A2A clients reject it. String substitution preserves the backend's
-- exact serialization and only touches the URL values.
local originals = {}
local function collect(u)
    if type(u) == "string" and u ~= "" and u ~= gateway_url then
        originals[u] = true
    end
end
collect(card.url)
local function collect_interfaces(list)
    if type(list) ~= "table" then return end
    for _, iface in ipairs(list) do
        if type(iface) == "table" then
            collect(iface.url)
        end
    end
end
collect_interfaces(card.additionalInterfaces)
collect_interfaces(card.supportedInterfaces)

-- Escape a string for use as a plain (non-pattern) gsub replacement: only "%"
-- is special on the replacement side.
local function escape_repl(s)
    return (s:gsub("%%", "%%%%"))
end
-- Escape Lua pattern magic chars so the search string matches literally.
local function escape_pat(s)
    return (s:gsub("([%^%$%(%)%%%.%[%]%*%+%-%?])", "%%%1"))
end

local rewritten = body
local gw_repl = escape_repl(gateway_url)
for original in pairs(originals) do
    rewritten = rewritten:gsub('"' .. escape_pat(original) .. '"', '"' .. gw_repl .. '"')
end
ngx.arg[1] = rewritten
