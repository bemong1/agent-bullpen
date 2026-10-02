# Viewing the dashboard from another device

By default the server listens on `127.0.0.1` only, with no login: only programs on the same machine can reach it. To look at it from a phone, a laptop or another computer you have two ways: give the server an address that other devices can reach (it then asks for an **access token**), or leave it on loopback and tunnel to it over SSH. The dashboard shows the conversations of every session it reads, so pick the way with that in mind.

## Opening it on an address: `--host` and the access token

`--host` takes the address to listen on. Any IP address works, and so does a host name that resolves on this machine; it can be repeated.

```bash
python3 server.py --host 0.0.0.0                       # every network card: the LAN and any VPN
python3 server.py --host 192.168.0.5                   # one address only
python3 server.py --host <vpn-address>                 # for example the 100.x address Tailscale or NetBird gave this machine
python3 server.py --host ::                            # IPv6 and IPv4 on every card
python3 server.py --host 127.0.0.1 --host <vpn-address>   # loopback without a login, the VPN address with the token
```

An address that is not loopback (`127.0.0.0/8`, `::1`, `localhost`) turns the token check on for that listener. The server makes a random token at every start (43 characters, from Python's `secrets`) and prints the addresses to open, with the token in them:

```
Dashboard — an access token is needed. Open one of these:
  http://localhost:8790/?token=Qw3dJ8…
  http://192.168.0.5:8790/?token=Qw3dJ8…
  http://100.64.0.2:8790/?token=Qw3dJ8…
The browser keeps the token in a cookie after the first visit, so later visits need no token. It travels unencrypted over http: use it on a network you trust (a VPN), or through an SSH tunnel.
```

For `0.0.0.0` and `::` the list is `localhost` and up to four IPv4 addresses of this machine's network interfaces (virtual bridges of containers and virtual machines are left out). Nothing is sent to find them. For a single address or a name, that one is printed. Use the address that the other device can reach.

### What happens on the other device

1. You open one of the printed addresses. The server checks the token, sets a cookie (`HttpOnly`, `SameSite=Strict`, a session cookie, named `agent_bullpen_<port>`) and answers with a redirect to the same address **without** the token. The token therefore does not stay in the address bar, the history or a bookmark.
2. From then on every page and every API call is answered when the browser sends that cookie. A reload, a new tab or a later visit needs nothing more, until the browser is closed or the server is started again. A restart ends every cookie it issued before, also with a fixed `--token`: open the address with `?token=` once more.
3. A request with no valid cookie, no `Authorization: Bearer <token>` header and no valid `?token=` is answered with **401**: a short note in English and Korean for a page, and `{"error": "unauthorized", "error_code": "unauthorized"}` for an API path. Nothing of the request is repeated in the answer.

The cookie value is not the token: it is derived from it, and from a salt made at each start, with a keyed hash, so no response ever contains the token. The salt is why a cookie does not outlive the server process even when the token is fixed. The token is compared in constant time. It is not written to the log (with `AGENT_BULLPEN_LOG` the request line of the first visit shows `token=…`). The cookie is per host name, not per port, so it is named after the port of the server that set it: two dashboards on one host, on different ports, do not overwrite each other's cookie.

A script can use the header instead of the cookie:

```bash
curl -H "Authorization: Bearer $TOKEN" http://192.168.0.5:8790/api/plans
```

### Fixing the token, or switching the check off

| | |
|---|---|
| `--token VALUE` | Use this token instead of a new random one at every start: at least 16 characters, letters, digits and `. _ ~ -` (it goes into an address as it is). The command line is visible in the process list, so for anything shared prefer the environment variable below. |
| `AGENT_BULLPEN_TOKEN` | The same, taken from the environment. The option wins when both are given. |
| `--no-auth` | No token check, even on a non-loopback address; it wins over `AGENT_BULLPEN_TOKEN` but cannot be combined with an explicit `--token`. The server prints a framed warning at start. Anyone who can reach the port can then read your conversations: use it only on a network you fully control. |

A token given by `--token` or `AGENT_BULLPEN_TOKEN` turns the check on for **every** listener, loopback included (useful on a machine other people log in to). Without a token, loopback stays as it always was: no login, and the `Host` header is checked against an allow list.

With several `--host` options the check is per listener: `--host 127.0.0.1 --host <vpn-address>` has no login on loopback and the token on the VPN address.

### Host names

Where a token is required, any `Host` name is accepted: the cookie is what protects you. A web page of another site that tries DNS rebinding against your server has no cookie for it. `--allow-host` therefore only matters where there is no token check (loopback, or `--no-auth`); there it works like this:

```bash
python3 server.py --host 0.0.0.0 --no-auth --allow-host my-machine.example.net
python3 server.py --host 0.0.0.0 --no-auth --allow-host .example.net     # every name under example.net
```

- `my.box` matches exactly that name; `.example.net` matches `a.example.net` and `a.b.example.net` but not `example.net` itself.
- The port in the header is not compared, and IP addresses are compared in canonical form: `fd7a:115c:a1e0:0:0:0:0:1` and `[fd7a:115c:a1e0::1]` are the same address.
- There is no built-in name or suffix.
- With `--no-auth`, a wildcard `--host` (`0.0.0.0`, `::`) accepts the names `localhost`, `127.0.0.1`, `::1` and `0.0.0.0` in the `Host` header, this machine's own IPv4 addresses (the ones printed at start) and the ones you add with `--allow-host`: a name that is not one of those (a DNS or VPN name, a reverse proxy) gets a 403 until you add it (`--allow-host my.box`).
- `--allow-host` on its own with a loopback server prints the framed "no authentication" warning. This is intended: a name you add is a name other machines are expected to use.

The 403 response says what to do. Its first line is English (a second line says the same in Korean):

```
Host not allowed: box.example.net. Restart the server with --allow-host box.example.net (or a suffix such as --allow-host .example.net).
```

## SSH tunnel (no option needed)

Keep the server on loopback (the default) and forward a port from the device you are sitting at:

```bash
ssh -L 8790:localhost:8790 you@the-machine      # then open http://localhost:8790
```

The server needs no extra option. It checks only the *name* in the `Host` header and ignores the port, so a different local port works too:

```bash
ssh -L 9000:localhost:8790 you@the-machine      # then open http://localhost:9000
```

Nothing is exposed to the network: the tunnel is authenticated and encrypted by SSH, and the server still listens on `127.0.0.1` only. On a network you do not trust this is the better way, because the token and the cookie of the `--host` way travel as plain http.

## Rules for opening it to the network

- **A network you trust only**: a private VPN (Tailscale, NetBird and the like) with an access policy that limits the port to your own devices, or a LAN you own. Not a shared, company, café or hotel network: the token and the cookie are readable by anyone who can see the traffic.
- **Never publish it**: no Tailscale Funnel, no ngrok or Cloudflare tunnel, no router port forward, no reverse proxy on a public host. A public URL would make your transcripts public, and the token is the only thing in front of them.
- Treat the printed addresses like a password: they hold the token. Do not paste them into a chat or an issue. A screenshot of the page is as sensitive as the conversations in it.
- A token that a start prints is valid until that server stops. Restart the server to invalidate it (a new random token), or change the one you fixed.

## If it does not open

| Symptom | Check |
|---|---|
| The server refuses to start: "Cannot listen on this address" | `--host` takes an IP address or a host name, with no port and no path. Brackets around an IPv6 address are fine (`--host [::]`). |
| "this machine has no such address" | The address is not one of this machine's (a VPN address disappears while the VPN is down). Check `--host`. |
| "the host name cannot be resolved" | The name given to `--host` is unknown here. |
| Connection refused | The server is running, the port is the one it printed, and the VPN is up on both ends. |
| 401 "Access token required" | Open the address with `?token=…` that the server printed at its start; a restart makes a new token, and with a fixed `--token` it still ends the cookies given before (the token in the address logs you in again). A page that was already open at the restart says "Token needed · reopen the printed address" in its header (the office page in its red line) and goes live again once this browser has the new token. If you clicked that address in another web page or a chat and still got the 401, reload once (the browser does not send a `SameSite=Strict` cookie on a navigation that started on another site), or paste the address into the address bar. |
| 403 "Host not allowed" | Only without a token check (loopback, `--no-auth`): add the name you typed with `--allow-host`. |
| Times out | The VPN's access policy or a firewall blocks the TCP port. |
| Page opens but is slow the first time | The bundled font is about 1.1 MB and is cached by the browser for a day. |
