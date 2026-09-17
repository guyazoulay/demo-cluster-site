# Demo Cluster Site

Double-click `start-demo-cluster-site.command` on macOS, then use the browser form at `http://127.0.0.1:8765`.

If macOS prevents the downloaded executable from opening, run this command from the project directory before launching it:

```sh
xattr -d com.apple.quarantine start-demo-cluster-site.command
```

The first cluster creation installs the required tooling through Homebrew (`node`, `mongosh`), npm (`m`), and an isolated Python virtual environment (`mlaunch`). Homebrew itself must already be installed.

All mlaunch files are stored in `~/.demo-cluster-site/data`. Creating a new cluster replaces only that directory and its MongoDB processes; it does not modify other MongoDB processes or data directories. Clusters use port `27017` when available, otherwise the first available contiguous local port range and display its connection string in the interface.

After a cluster is ready, use **Add additional data** to populate a new collection in the `demo` database. Provide a collection name, comma-separated field names, and an approximate size from 1 MB to 500 MB; the load runs in the background.
