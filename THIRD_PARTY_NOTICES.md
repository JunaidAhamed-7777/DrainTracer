# Third-party notices

Last reviewed: **28 October 2026**.

DrainTracer's own code and accompanying documentation are licensed under
[MIT](LICENSE), copyright 2026 Junaid Ahamed, except where a file identifies
other rights. Dependency authors retain their own copyrights and license
conditions. This inventory identifies the principal runtime dependencies;
it does not replace the complete license files in the exact distributions
you install or redistribute.

## Direct runtime dependencies

The supported version ranges and Python conditions are in
[requirements.txt](requirements.txt).

- **psutil**: process and system measurements, under **BSD-3-Clause**.
  [Source and license](https://github.com/giampaolo/psutil/blob/master/LICENSE).
- **Rich**: terminal presentation, under **MIT**.
  [Source and license](https://github.com/Textualize/rich/blob/main/LICENSE).
- **Tomli**: TOML parsing on Python versions below 3.11, under **MIT**.
  [Source and license](https://github.com/hukkin/tomli/blob/master/LICENSE).
  Python 3.11 and later use the standard library's `tomllib` instead.

## Indirect runtime dependencies

The resolved dependency set depends on Python and the installed versions.
The current Rich dependency chain includes:

- **markdown-it-py**, under **MIT**, with a separate upstream markdown-it
  attribution/license notice.
  [Python-port license](https://github.com/executablebooks/markdown-it-py/blob/master/LICENSE)
  and [upstream notice](https://github.com/executablebooks/markdown-it-py/blob/master/LICENSE.markdown-it).
- **mdurl**, under **MIT**, including its upstream attribution.
  [Source and license](https://github.com/executablebooks/mdurl/blob/master/LICENSE).
- **Pygments**, under **BSD-2-Clause**.
  [Source and license](https://github.com/pygments/pygments/blob/master/LICENSE).

This is not a locked bill of materials. Older supported versions or additional
environment packages can have other dependencies, notices, or vendored
components. Review the actual installed metadata and files, including
additional notices in each distribution, before distributing a bundle.

## Python, Windows, and build tools

Python is installed separately and has its own
[license and third-party notices](https://docs.python.org/3/license.html).
Windows and its APIs remain subject to Microsoft's applicable terms; using
those APIs does not imply Microsoft sponsorship. DrainTracer's normal pip
package does not include a Python interpreter or Windows runtime.

Build and development tools, such as setuptools, pip, Git, and any tools you
add to your build, are separately licensed. If you redistribute any of them,
include the notices and satisfy the conditions for those exact versions too.

## Redistributing software or generated reports

Preserve DrainTracer's MIT copyright and permission notice. When copying or
bundling dependencies, preserve each component's required copyright,
permission, disclaimer, and attribution notices; do not label the whole
bundle as exclusively DrainTracer-authored or exclusively MIT-licensed.
BSD components have their own redistribution conditions, including required
notices, and BSD-3-Clause includes a non-endorsement condition. Refer to the
full upstream licenses for their exact requirements.

Package installers normally install dependencies as separate distributions
with their own notices. A frozen executable, embedded interpreter, installer,
or copied source tree may need a different notice layout. Inspect the actual
artifact; a link to this inventory alone is not a substitute for preserving
required license text.

Generated HTML reports embed DrainTracer's report template, CSS, JavaScript,
and the user's data. They do not download a third-party chart library at
viewing time. Include the applicable MIT notice when redistributing a
substantial copy of the report template or code. The template's license does
not claim ownership of the user's measurements or make confidential report
data public. Privacy and licensing are separate obligations.

For a missing notice or licensing concern, use the
[legal contact route](LEGAL.md#contact) with the component name, version,
affected file, and supporting license source. Do not include confidential
material in a public request.
