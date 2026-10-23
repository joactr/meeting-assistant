fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    #[cfg(unix)]
    if args.first().map(String::as_str) == Some("--engine-supervisor") {
        if let Err(error) = native_host::supervisor::unix_watchdog() {
            eprintln!("{error}");
            std::process::exit(1)
        }
        return;
    }
    match native_host::acceptance::run(&args) {
        Ok(value) => println!("{value}"),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    }
}
