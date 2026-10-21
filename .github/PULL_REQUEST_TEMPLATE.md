## 问题与结果

说明触发条件、修改后的行为和仍未完成的部分。

## 来源与验证

- 来源：device1 / device2 / cloud；基线提交与最终版本：
- 验证：全套离线 unittest、`git diff --check` 及相关证据：
- 若涉及仪器：模式、已验收站点、尚未验收范围；fake 通过不等于 live 通过。
- 若涉及知识/数据：来源、适用范围和原始证据引用。

device1 与 device2 使用相同合入标准。合入后只清理已被 main 包含的主题分支，
现场部署另记录实际 SHA。流程见[开发协作](../docs/operations/development-workflow.md)。
