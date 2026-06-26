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
-- http_host preserves a non-default port (e.g. :8443); ngx.var.host strips it.
-- Trailing slash so the advertised URL matches the JSON-RPC endpoint, which is
-- the prefix location {ROOT_PATH}/agent/<path>/ (a no-slash URL would not match).
local gateway_url = ngx.var.scheme .. "://" .. ngx.var.http_host .. base .. "/"

if card.url then
    card.url = gateway_url
end
-- A2A advertises extra transports under different keys across versions:
-- "additionalInterfaces" (0.2.x) and "supportedInterfaces" (proto/1.0).
-- Point every advertised interface URL at the gateway.
local function rewrite_interfaces(list)
    if type(list) ~= "table" then return end
    for _, iface in ipairs(list) do
        if type(iface) == "table" and iface.url then
            iface.url = gateway_url
        end
    end
end
rewrite_interfaces(card.additionalInterfaces)
rewrite_interfaces(card.supportedInterfaces)

local eok, encoded = pcall(cjson.encode, card)
if eok then
    ngx.arg[1] = encoded
else
    ngx.arg[1] = body
end
