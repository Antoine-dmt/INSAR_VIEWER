# INSAR_VIEWER Project Plan

## Scope

Separate public Antoine-dmt/INSAR_VIEWER repository, installed beside the
processing repository at SCIENCE/INSAR_VIEWER. Preserve the existing viewer's
interface and scientific features without importing the processing pipeline.
Read-only existing geocoded MintPy/ERA5 products; no scientific processing,
data acquisition, credentials, or real observations in the public repository.

## Workstreams

| Work | Status | Acceptance |
| --- | --- | --- |
| Architecture and scientific input contract | Reviewed | Explicit arbitrary file paths, geometry conventions, correction declarations and separate publication qualification |
| Portable backend and source protection | Implemented; backend tests passed | Standalone reader; synthetic source hashes unchanged after rendering; explicit grid/network/mask checks |
| Existing UI and distributable package | Implemented; wheel build passed | Both tabs, maps/time series, GNSS uncertainties, zoom/pan, references and exports; UI bundled in wheel |
| Backend regression verification | Passed (reported by coordinator) | 19 passed, 2 opt-in browser tests skipped, 7 subtests; 4.41 seconds |
| Browser verification | Passed | 2 final browser tests passed in 17.55 seconds: desktop/mobile maps, charts, GNSS, references, zoom/pan, exports and restricted static routes |
| Documentation | Updated locally | Frozen-backend contract, pip/Conda setup, nohup operation, scientific limits, synthetic demo and private-data handling |
| Documentation-code audit | Reviewed | Correction declarations are optional and explicitly described as provider assertions, not an ERA5 verification gate |
| Requested local folder | Created and populated | Separate `SCIENCE/INSAR_VIEWER` checkout, without processing inputs or private institute configuration |
| Public GitHub remote | Verified and attached | `https://github.com/Antoine-dmt/INSAR_VIEWER.git`; separate local `main` branch |
| Local main commit and push | Initial commit created; push authentication blocked | Public-safe source committed on `main` using GitHub noreply identity; push from an authenticated VS Code session is still required |
| Field-data scientific validation | Not performed | Synthetic tests and successful rendering do not qualify products for publication |

## Scientific Limits

ERA5 identity comes from an explicit provider declaration, not a filename.
The parser and reader do not require an ERA5 declaration; operational readability
does not establish correction provenance or publication readiness. Source
displacement is in metres; display/export displacement is in centimeters,
positive toward the satellite. MintPy ground-to-satellite azimuth is
counterclockwise from north, in degrees.

East/up decomposition assumes zero north motion and requires the same group,
mission and AOI. Unknown networks and windows crossing disconnected/interleaved
components are blocked. Actual HDF5 reference locations are checked; mismatches
require one explicit common viewer reference. Per-orbit LOS offsets are removed
on native grids before reprojection and inversion, including varying geometry.

Geocoded metadata is required; FIRST coordinates denote the outer corner and
pixel locations use centers. Valid different grids/CRSs are supported through
DESC-to-ASC reprojection. Quality-stack averages preserve valid zero values;
explicit nodata attributes and finite, threshold-passing coherence from both
orbits govern masks. Geometry is optional for LOS maps. Synthetic demo products
are neither observations nor an ERA5 computation.

## Verification

Documentation was checked against the frozen config, reader, CLI, geometry,
quality/reference/reprojection code and focused regression tests. Backend and
wheel outcomes above were supplied by the coordinator. The final extended
browser run passed on desktop and mobile viewports with device pixel ratio 2.

The original INSAR worktree is confirmed clean. Source-file preservation is
covered by synthetic SHA-256 tests. Real observations, HDF5 products and
credentials are excluded from the public deliverable. Git authentication tests
previously failed: the shell did not inherit VS Code OAuth. The public remote
now exists and is attached as `origin`. The direct push failed because the
shell cannot read a GitHub username; VS Code Source Control Push or a manually
authenticated terminal is required. The local commit uses a GitHub noreply
address rather than the user's private email.
