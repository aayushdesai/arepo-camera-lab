#!/bin/bash
#SBATCH --job-name=mesh_fidelity_721
#SBATCH --partition=defaultp
#SBATCH --constraint=eta
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=00:10:00
set -eo pipefail
test -n "${SLURM_JOB_ID:-}" || { echo 'Requires a Slurm allocation'; exit 2; }
case "$(hostname -s)" in eta*) ;; *) echo 'Requires eta'; exit 2;; esac
source /nfs/scistore25/caiazgrp/adesai/Arepo/load_arepo_batch_env.sh
test "${CONDA_DEFAULT_ENV:-}" = Arepo_Env || { echo 'Arepo_Env inactive'; exit 2; }
set -u
export OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
python -u audit_mesh.py --config config.json
