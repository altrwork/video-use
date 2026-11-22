Fonts used by the caption looks in `helpers/caption_looks.py`. All are under the SIL Open Font License 1.1 (the `OFL-*.txt` file next to each family). Copied unmodified from the Subtle repository (https://github.com/0xpratzyy/subtle), which took them from https://github.com/google/fonts; Inter and Montserrat are its static weight cuts.

render.py passes this folder (and the client's `brand/fonts/`) to libass, so nothing has to be installed system-wide.

## Caption look definitions

The looks in `helpers/caption_looks.py` adapt style definitions from Subtle, under this licence:

```
MIT License

Copyright (c) 2026 Refs (refs.video)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
