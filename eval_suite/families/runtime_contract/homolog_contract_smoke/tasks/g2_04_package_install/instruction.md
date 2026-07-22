A small Python script needs a third-party library that is not yet installed.
Install the `cowsay` package using pip, then run:

```
python3 -c "import cowsay; cowsay.cow('hello')" > cowsay_output.txt
```

in the current working directory so that `cowsay_output.txt` contains the
cow's output. Then signal that the task is complete.
