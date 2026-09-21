# Third-party notices

The repository includes the following vendored browser assets for the optional desktop workbench. Their original license headers are retained in the distributed files.

| Component | Version in repository | License | Location |
| --- | --- | --- | --- |
| Vue | 3.4.38 | MIT | `desktop/frontend/vendor/vue.global.prod.js` |
| Apache ECharts | 5.5.1 | Apache-2.0 | `desktop/frontend/vendor/echarts.min.js` |

All Python dependencies are resolved from the lock files (`uv.lock` and `desktop/uv.lock`) at installation time. Their license obligations remain with their respective licensors.

Complete upstream texts are distributed under `desktop/frontend/vendor/licenses/`:

- [Vue MIT license](desktop/frontend/vendor/licenses/vue-LICENSE.txt), from `vuejs/core` tag `v3.4.38`.
- [ECharts license](desktop/frontend/vendor/licenses/echarts-LICENSE.txt) and [NOTICE](desktop/frontend/vendor/licenses/echarts-NOTICE.txt), from `apache/echarts` tag `5.5.1`.
- [D3 BSD license](desktop/frontend/vendor/licenses/echarts-d3-LICENSE.txt) for the ECharts subcomponents identified by its upstream license.

These files were retrieved from the corresponding upstream tagged repositories on 2026-09-21. They supplement, and do not replace, the license headers in the vendored assets.
