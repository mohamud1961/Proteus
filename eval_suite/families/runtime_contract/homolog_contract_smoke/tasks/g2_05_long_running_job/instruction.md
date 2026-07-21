Launch a background job that sleeps for 10 seconds and then writes the line
`job complete` to a file named `done.txt` in the current working directory.
Wait for that background job to finish, polling its status as needed. Once
it has finished and `done.txt` exists, signal that the task is complete.
