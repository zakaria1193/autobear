# autocompdb development

- Run `python -m unittest discover -s tests -v` before changing capture or repair behavior.
- Use recorded provider responses for tests.
- Check Bear options and schema against the supported Bear release. Use real Bear to test capture and repair.
- Keep compiler invocations as argument arrays. Pass repair commands to `bear parse-sh` as text.
- Preserve existing compilation entries and user clangd settings.
