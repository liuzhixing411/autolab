## CFD: health checks instead of W&B
For solver runs, check residual histories, Courant number, conservation errors and monitor-point convergence from the job log / postProcessing files (see the domain field notes). Flag oscillating residuals or a Courant number above the scheme's limit as a failed sanity check.
