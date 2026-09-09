# Task 6A — Remotion production contract

Date: 2026-09-09

## Scope

Remotion production registration and input contract only. No frontend, backend,
approved asset inventory, provider, deployment, or file deletion work.

## TDD record

### RED

Added `remotion/src/productionContract.test.mjs` first and ran:

```text
node --test src/productionContract.test.mjs
```

The three tests failed for the expected reasons: `Root.tsx` still contained
`sampleInput` and `defaultProps`, the demo preview was registered in Root, and
the explicit input validator did not exist.

### GREEN

Implemented the smallest contract cleanup:

- Removed the sample render payload and all production `defaultProps`.
- Added registration-only placeholder dimensions; `calculateMetadata` now
  requires and derives metadata from injected props.
- Added `requireExplicitRenderInput()` to reject missing or malformed real
  render input before composition code runs.
- Wrapped both registered production compositions with the explicit-input
  guard. The backend composition ID `VisualSystemV3Full` is unchanged.
- Removed `VisualSystemV3Preview` from Root and marked its retained fixed-copy
  implementation `DEMO-ONLY`; approved assets were not changed or deleted.

Verification:

```text
npm test                 # 37 passed
npm run typecheck        # exit 0
```

`npx remotion compositions src/index.ts` bundled the source but could not
finish because the sandbox denied writes under `remotion/node_modules` and
Remotion attempted to download its headless browser. No live or paid provider
was called.

## Files changed

- `remotion/src/Root.tsx`
- `remotion/src/VisualSystemV3Preview.tsx`
- `remotion/src/VisualSystemV3Full.tsx`
- `remotion/src/types.ts`
- `remotion/src/productionContract.test.mjs`
- `task-6a-report.md`

## Residual risks / follow-up

- `npm run render:fixture` no longer has an implicit fixture; callers must
  provide an explicit `--props` JSON payload, as required by production
  safety. A separate explicitly named demo/fixture command can be added later
  if desired.
- The retained preview file still contains fixed demo copy/assets by design;
  it is quarantined by documentation and is no longer imported or registered
  by the production Root.
- Runtime composition enumeration was not completed because of the local
  Remotion browser/cache permission failure; typecheck and all hermetic tests
  passed.
