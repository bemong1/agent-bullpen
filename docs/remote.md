# Viewing the dashboard from another device

Agent Bullpen has **no login**. Whoever can open its port can read the conversations of every session it shows. Pick the way of reaching it with that in mind.

## SSH tunnel (recommended)

Keep the server on loopback (the default) and forward a port from the device you are sitting at:

```bash
ssh -L 8790:localhost:8790 you@the-machine      # then open http://localhost:8790
```

The server needs no extra option. It checks only the *name* in the `Host` header and ignores the port, so a different local port works too:

```bash
ssh -L 9000:localhost:8790 you@the-machine      # then open http://localhost:9000
```

Nothing is exposed to the network: the tunnel is authenticated by SSH, and the server still listens on `127.0.0.1` only.

## Opening it on a VPN address

If the machine and the other device share a private VPN (Tailscale, NetBird and the like), you can also listen on the machine's VPN address in addition to loopback:

```bash
python3 server.py --host 127.0.0.1 --host <vpn-address>
```

`--host` accepts only loopback and the VPN ranges `100.64.0.0/10` (CGNAT, used by Tailscale and NetBird) and `fd7a:115c:a1e0::/48` (Tailscale IPv6). `0.0.0.0` and ordinary LAN addresses (`192.168.x.x`, `10.x.x.x`, `172.16.x.x`) refuse to start. Listening on an address that does not exist on the machine (for example a VPN that is down) also fails, with a message saying so.

Starting with any non-loopback address prints this warning on stderr (in English, or in your language when the terminal language is set to Korean; see [Language](configuration.md#language)):

```
Warning: no authentication — this server has no login. Every device that can reach the addresses below can read your Claude and Codex conversations (access control is up to your VPN and firewall): listening on <addresses>
```

The end of the line lists what is exposed: `listening on …` for `--host` addresses, `allowed names …` for `--allow-host` names.

Rules for this mode:

- **Private VPN only**, with access limited to devices you own (use the VPN's access policy to restrict the port). Do not put the server on a LAN, a shared or company network, or a hotspot.
- **Never publish it**: no Tailscale Funnel, no ngrok or Cloudflare tunnel, no router port forward, no reverse proxy on a public host. A public URL would make your transcripts public.
- Treat a screenshot of the page as sensitive: it shows prompts and file names.

## Reaching it by name: `--allow-host`

Browsing to `http://<vpn-address>:8790` works as is, because every `--host` address is accepted. Browsing by a *name* (a VPN host name, an `/etc/hosts` alias, a reverse proxy) is rejected with a 403 until you list the name. This is a defense against DNS rebinding, where a web page on another site makes your browser talk to your local server under that site's name.

```bash
python3 server.py --host 127.0.0.1 --host <vpn-address> --allow-host my-machine.example.net
python3 server.py --host 127.0.0.1 --host <vpn-address> --allow-host .example.net     # every name under example.net
```

- `my.box` matches exactly that name; `.example.net` matches `a.example.net` and `a.b.example.net` but not `example.net` itself.
- The port in the header is not compared, and IP addresses are compared in canonical form: `fd7a:115c:a1e0:0:0:0:0:1` and `[fd7a:115c:a1e0::1]` are the same address.
- There is no built-in name or suffix.
- `--allow-host` on its own, even with the default loopback listener, prints the same "no authentication" warning above. This is intended: a name you add is a name other machines are expected to use.

The 403 response says what to do. Its first line is English (a second line says the same in Korean):

```
Host not allowed: box.example.net. Restart the server with --allow-host box.example.net (or a suffix such as --allow-host .example.net).
```

## If it does not open

| Symptom | Check |
|---|---|
| Connection refused | The server is running, and the VPN is up on both ends. Start-up prints each address it opened. |
| 403 "Host not allowed" | Add the name you typed with `--allow-host` (above). |
| Times out | The VPN's access policy or a firewall blocks the TCP port. |
| Page opens but is slow the first time | The bundled font is about 1.1 MB and is cached by the browser for a day. |
