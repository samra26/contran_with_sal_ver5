# convtran_with_sal_final_ver1
python main.py --mode=test --sal_mode=SIP --test_root=../testsod/STERE/STERE --test_list=../testsod/STERE/STERE/test.lst --test_folder=./test/STERE --model=./checkpoints/demo-17/epoch_90.pth

python main.py --mode=test --sal_mode=NLPR --test_root=../testsod/NLPR/NLPR --test_list=../testsod/NLPR/NLPR/test.lst --test_folder=./test/NLPR --model=./checkpoints/demo-17/epoch_90.pth

python main.py --mode=test --sal_mode=NLPR --test_root=../testsod/NLPR/NLPR --test_list=../testsod/NLPR/NLPR/test.lst --test_folder=./test/NLPR --model=./checkpoints/demo-20-1/demo-20/final.pth --batch_size=1

python main.py --mode=test --sal_mode=STERE --test_root=../testsod/STERE/STERE --test_list=../testsod/STERE/STERE/test.lst --test_folder=./test/STERE --model=./checkpoints/demo-20-1/demo-20/final.pth --batch_size=1
