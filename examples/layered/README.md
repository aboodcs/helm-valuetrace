# Layered values example

Run these commands from the repository root after `./install-local.sh`.
All credentials are deliberately fake. No cluster is required.

```sh
helm valuetrace examples/layered/chart \
  -f examples/layered/development.yaml -f examples/layered/production.yaml
```

Exit `0`. `replicaCount` is `4`, `image.tag` is `production`, and their
winning source is `examples/layered/production.yaml` (lines 1 and 3).
The password and the token inside `users` are redacted.

```sh
helm valuetrace examples/layered/chart \
  -f examples/layered/development.yaml -f examples/layered/production.yaml \
  --set-string image.tag=007 --explain image.tag
```

Exit `0`. Final value is the string `007`, from `--set-string[1]`.
History contains `stable`, `development`, `production`, then `007`.

```sh
helm valuetrace examples/layered/chart \
  -f examples/layered/development.yaml -f examples/layered/reset.yaml \
  -f examples/layered/restore.yaml --explain service.type --output json
```

Exit `0`. Final value is `ClusterIP`, supplied by chart defaults at line 7.
History also shows the earlier `LoadBalancer` and the parent replacement
`disabled`; those assignments do not supply the effective value.

```sh
helm valuetrace examples/layered/chart \
  -f examples/layered/development.yaml -f examples/layered/production.yaml --output json
helm valuetrace examples/layered/chart --output yaml
helm valuetrace examples/layered/chart --set replicaCount=0 --strict-schema
helm valuetrace examples/layered/chart --set image.repostory=oops --strict-schema
helm valuetrace examples/layered/chart -f examples/layered/development.yaml \
  --deny-source '*development*'
```

The first two commands exit `0` and produce parseable reports. The last three
exit `2`: minimum constraint, forbidden property, and denied source respectively.
Schema diagnostics identify the constraint and schema path without echoing values.
With a schema, use `--strict-schema`; `--strict-unknown` is the default-key
heuristic used when there is no schema.

To verify the effective values independently:

```sh
helm template example examples/layered/chart \
  -f examples/layered/development.yaml -f examples/layered/production.yaml
```

Helm's ConfigMap contains the same effective values, including the fake credentials
in plaintext. ValueTrace redacts only its own reports, not Helm output.

Primary explain syntax: `helm valuetrace CHART --explain KEY`. For example:

```bash
helm valuetrace examples/layered/chart --explain image.tag
helm valuetrace examples/layered/chart --deny-cli-overrides -f examples/layered/production.yaml
helm valuetrace -V
```

`CHART explain KEY` remains compatible. `explain KEY CHART` is not supported.
