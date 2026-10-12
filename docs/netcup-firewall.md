# Netcup firewall procedure

Netcup is managed in two ways (v0.4). Either the SCP REST adapter automates the
firewall (see [providers.md](providers.md)) once `config/providers/netcup.yaml`
is filled in from the SCP API docs — or, while that file is absent, `just
server-up` prints the rule table and pauses for you to apply it by hand. From
[spec.md §7.4](spec.md#74-provider-firewall-compute-slot).

## Rule table (the same on every provider)

| Direction | Proto | Src port | Dst port | Action | Note |
|---|---|---|---|---|---|
| in | UDP | 53 | * | ACCEPT | DNS replies (needed on stateless firewalls) |
| in | UDP | 123 | * | ACCEPT | NTP replies |
| in | UDP | 7844 | * | ACCEPT | cloudflared QUIC replies |
| in | UDP | * | 41641 | ACCEPT | Tailscale direct path |
| in | ICMP/ICMPv6 | — | — | ACCEPT | PMTU and IPv6 neighbour discovery |
| in | TCP | * | * | DROP | |
| in | UDP | * | * | DROP | |
| out | TCP | * | 25, 465, 587 | DROP | Provider default; outbound email goes through an HTTPS API |

## Order of operations

1. In the SCP, apply the rule table above, **and temporarily allow inbound TCP 22**.
2. Run `just server-up server-1`. It reads the public IP from `SERVER_IP_SERVER_1`
   (a bootstrap secret, D-049). Bootstrap replaces password auth
   with your key and joins Tailscale.
3. When `just` pauses after bootstrap, **remove the temporary TCP 22 rule** in the
   SCP and confirm.
4. The rest of the run happens over Tailscale.

## Verify the origin is sealed

From an external network, confirm these time out on the public IP:

```
nc -zv <public-ip> 22
nc -zv <public-ip> 80
nc -zv <public-ip> 443
nc -zv <public-ip> 3000
```

All four must fail. `ssh server-1` (over Tailscale) must still work, and
`http://server-1:3000` must show the Dokploy panel.

## Defence in depth

The provider firewall is the outer layer. On the host, UFW denies incoming by
default and a `DOCKER-USER` block closes Docker's bypass of UFW (D-005), so
published container ports are not reachable on the WAN interface even if the SCP
rules are wrong.
