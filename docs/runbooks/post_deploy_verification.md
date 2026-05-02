# Post-deploy verification checklist (production)

> Run this within 5 minutes of `systemctl restart sheeza.service`.
> If anything is red, follow `docs/runbooks/rollback.md`.

## A. Service is up

```
[ ] systemctl is-active sheeza.service           → active
[ ] systemctl show sheeza.service \
       -p ActiveEnterTimestamp --value           → matches the
                                                   restart you just ran
[ ] journalctl -u sheeza.service -n 200 \
       | grep -E 'Traceback|ERROR'               → empty
```

## B. HTTP is up

```
[ ] curl -sk -o /dev/null -w '%{http_code}\n' \
       https://sheezamanzil.com/                  → 200
[ ] curl -sk -o /dev/null -w '%{http_code}\n' \
       https://sheezamanzil.com/appadmin          → 200
[ ] curl -sk https://sheezamanzil.com/admin/diag \
       | grep -oE 'build [a-f0-9]+'              → matches
                                                   the deployed sha
```

## C. Migration head is correct

```
[ ] flask --app run.py db current                 → expected head
[ ] flask --app run.py db heads                   → single head
[ ] No "Multiple heads" warning in the output.
```

## D. Operator-flow smoke

Log in as a real operator account. Click each:

```
[ ] /dashboard/                                   → renders
[ ] /front-office/                                → counts look sane
[ ] /front-office/arrivals                        → today's arrivals
                                                   render with names
[ ] /front-office/departures                      → today's departures
[ ] /board/  (if shipped to prod)                 → drag/drop works
[ ] /bookings/                                    → recent bookings
[ ] /invoices/                                    → recent invoices,
                                                   totals look sane
[ ] /admin/whatsapp                               → inbox renders
                                                   (if WhatsApp is in
                                                   the deploy)
```

## E. Key data sanity

```
[ ] psql counts have not regressed:
       SELECT 'bookings', COUNT(*) FROM bookings;
       SELECT 'invoices', COUNT(*) FROM invoices;
       SELECT 'users',    COUNT(*) FROM users;
[ ] SELECT MAX(created_at) FROM activity_logs;    → very recent
[ ] No orphaned bookings:
       SELECT COUNT(*) FROM bookings WHERE guest_id IS NULL;
       (should be 0 unless documented otherwise)
```

## F. Channel manager (if deployed to prod)

```
[ ] /admin/channels/                              → renders
[ ] /admin/channel-exceptions/                    → renders
                                                   (queue may be empty)
[ ] No new exceptions with issue_type='parse_error' since the
    restart (which would suggest the deploy broke a payload
    parser).
```

## G. WhatsApp (if deployed and live)

```
[ ] Send a test message from a known sandbox number.
[ ] /admin/whatsapp shows the inbound message within 10 seconds.
[ ] AI draft is generated (or the mock placeholder if
    AI_DRAFT_PROVIDER=mock).
```

## H. Final hygiene

```
[ ] Update docs/handoff/current_state.md with the new prod sha.
[ ] Tag the deploy:
       git tag -a prod-$(date -u +%Y-%m-%d) -m "summary"
       git push origin prod-$(date -u +%Y-%m-%d)
[ ] Note any quirks in docs/handoff/known_bugs.md.
[ ] Watch journalctl -u sheeza.service -f for 5 more minutes.
```

## If anything in A–G is red

Stop. Open `docs/runbooks/rollback.md` and decide whether you need
section A (code-only rollback) or section B (DB rollback). Don't
"try one more thing" — production is live customers.
